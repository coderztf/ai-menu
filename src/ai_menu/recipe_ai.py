"""Configurable recipe candidate generation through a chat-completions endpoint."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from .paths import DEFAULT_AI_CONFIG

import yaml
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class RecipeAIError(RuntimeError):
    def __init__(self, message, *, reason="invalid_response", upstream_status=None):
        super().__init__(message)
        self.reason = reason
        self.upstream_status = upstream_status


@dataclass(frozen=True)
class ChatRecipeGenerator:
    endpoint: str
    model: str
    api_key: str | None = None

    @classmethod
    def from_environment(cls, config_path=None, *, section="ai", prefix="MENU_AI"):
        path = Path(config_path) if config_path is not None else DEFAULT_AI_CONFIG
        config = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as source:
                    document = yaml.safe_load(source)
            except yaml.YAMLError:
                raise ValueError("Invalid YAML in ai-config.yaml") from None
            if document is None:
                document = {}
            if not isinstance(document, dict):
                raise ValueError("ai-config.yaml must contain a mapping")
            config = document.get(section, {})
        if not isinstance(config, dict):
            raise ValueError(f"ai-config.yaml must contain a {section} mapping")
        values = {}
        for field in ("url", "model", "api_key"):
            variable = f"{prefix}_KEY" if field == "api_key" else f"{prefix}_{field.upper()}"
            value = os.environ.get(variable, config.get(field, ""))
            if not isinstance(value, str):
                raise ValueError(f"AI configuration field {field} must be a string")
            values[field] = value.strip()
        endpoint, model = values["url"], values["model"]
        if not endpoint and not model:
            return None
        if urlsplit(endpoint).scheme not in ("https", "http") or not urlsplit(endpoint).hostname or not model:
            raise ValueError(f"Set {section} url and model in ai-config.yaml or {prefix}_URL and {prefix}_MODEL")
        return cls(endpoint, model, values["api_key"] or None)

    def generate(self, ingredients, excluded_names):
        instructions = (
            "你负责给三人家庭设计家常菜。仅用输入清单中的食材代码，"
            "可假设有水和普通食用油。只返回 JSON 对象，格式为 "
            '{"recipes":[{"name":"菜名","kind":"dish","minutes":15,"instructions":["步骤"],'
            '"ingredients":[{"code":"清单中的代码","amount_hint":"三人份用量"}]}]}。'
            "提供最多八道不同的菜，覆盖含肉菜、纯素菜和蛋菜，排除汤、主食和已有菜名。"
            "不要使用用完或未录入食材。少量食材只可少量配菜，"
            "后端第一版会保守排除用到少量食材的菜，优先用充足和常备食材。"
            "所有烹饪中用到的受限食材必须列在 ingredients 中。"
        )
        return self.complete(
            instructions, {"ingredients": ingredients, "exclude": excluded_names},
            timeout=None,
        )

    def parse_inventory(self, text, known_names):
        return self.complete(
            '提取用户原文明确提到的库存食材。只返回 JSON 对象 '
            '{"items":[{"name":"食材名","state":"enough","note":null,"prioritize":false}],'
            '"unresolved":["无法判断的原文片段"]}。'
            '参考已知名称和别名，允许新食材。非库存内容放入 unresolved。'
            '状态只允许 enough、low、empty。不少、大量、未写数量的明确补货为 enough，'
            '一些、一点、明确只剩少量为 low，没有或用完为 empty。'
            '备注保留明确数量和单位，优先使用必须由用户明示。'
            'empty 的备注必须是 null，prioritize 必须是 false。保留重复项，勿猜测或添加食材。',
            {"text": text, "known_names": known_names},
        )

    def complete(self, instructions, data, *, timeout=35, json_mode=False):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, json.dumps(payload).encode(), headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read(512_001))
            content = data["choices"][0]["message"]["content"]
            content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content, flags=re.I)
            return json.loads(content)
        except HTTPError as error:
            reason = "upstream_http"
            if error.code == 429:
                try:
                    body = json.loads(error.read(8192))
                    details = body.get("error") if isinstance(body, dict) else None
                    if isinstance(details, dict) and (
                        details.get("code") in ("credit_balance_exhausted", "insufficient_quota")
                        or details.get("type") == "insufficient_quota"
                    ):
                        reason = "insufficient_quota"
                except (OSError, ValueError, TypeError):
                    pass
            raise RecipeAIError("Model HTTP error", reason=reason,
                                upstream_status=error.code) from error
        except TimeoutError as error:
            raise RecipeAIError("Model request timed out", reason="timeout") from error
        except URLError as error:
            reason = "timeout" if isinstance(error.reason, TimeoutError) else "connection_failed"
            raise RecipeAIError("Model connection failed", reason=reason) from error
        except OSError as error:
            raise RecipeAIError("Model connection failed", reason="connection_failed") from error
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise RecipeAIError("Model response could not be decoded") from error


def validate_candidates(data, available_codes, excluded_names):
    if not isinstance(data, dict) or not isinstance(data.get("recipes"), list):
        raise RecipeAIError("Missing recipe list")
    names = {name.strip() for name in excluded_names}
    valid = []
    for row in data["recipes"][:12]:
        if not isinstance(row, dict):
            continue
        name, minutes = row.get("name"), row.get("minutes")
        steps, ingredients = row.get("instructions"), row.get("ingredients")
        if (row.get("kind") != "dish" or not isinstance(name, str) or not name.strip() or len(name.strip()) > 100
                or name.strip() in names or re.search(r"汤$|粥$|面条$|炒饭$|盖饭$|饺子$|馒头$", name.strip())
                or type(minutes) is not int or not 1 <= minutes <= 180
                or not isinstance(steps, list) or not 1 <= len(steps) <= 12
                or any(not isinstance(step, str) or not step.strip() or len(step) > 400 for step in steps)
                or not isinstance(ingredients, list) or not 1 <= len(ingredients) <= 20):
            continue
        uses = {}
        for item in ingredients:
            if not isinstance(item, dict):
                break
            code, amount = item.get("code"), item.get("amount_hint")
            if (not isinstance(code, str) or code not in available_codes or code in uses
                    or not isinstance(amount, str) or not amount.strip() or len(amount) > 80):
                break
            uses[code] = amount.strip()
        else:
            names.add(name.strip())
            valid.append((name.strip(), minutes, [step.strip() for step in steps], uses))
    if data["recipes"] and not valid:
        raise RecipeAIError("No generated recipe passed validation", reason="invalid_candidates")
    return valid
