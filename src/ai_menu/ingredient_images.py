"""Generate a food thumbnail with a configured Images API."""

from base64 import b64decode
from dataclasses import dataclass, replace
import json
import re
from time import sleep
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .recipe_ai import ChatRecipeGenerator, RecipeAIError


@dataclass(frozen=True)
class IngredientImageGenerator(ChatRecipeGenerator):
    @classmethod
    def from_environment(cls, config_path=None):
        client = super().from_environment(config_path, section="image_ai", prefix="MENU_IMAGE")
        if client is None:
            return None
        url = urlsplit(client.endpoint)
        if (url.hostname in {"api.moonshot.cn", "api.moonshot.ai"}
                and url.path.rstrip("/") == "/v1/images/generations"):
            client = replace(client, endpoint=urlunsplit(url._replace(path="/v1/chat/completions")))
        return client

    @property
    def extension(self):
        return "svg" if urlsplit(self.endpoint).path.rstrip("/").endswith("/chat/completions") else "webp"

    def generate(self, name):
        if self.extension == "svg":
            return self.generate_svg(name)
        payload = json.dumps({
            "model": self.model,
            "prompt": f"Square studio photograph of the raw cooking ingredient named {json.dumps(name)}. "
                      "Show only the ingredient, centered, with a warm ivory background and soft shadows. "
                      "No text, packaging, utensils, or cooked food.",
            "size": "1024x1024",
            "quality": "low",
            "output_format": "webp",
            "output_compression": 75,
            "n": 1,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, payload, headers, method="POST")
        try:
            with urlopen(request, timeout=120) as response:
                result = json.loads(response.read(4_000_001))
            image = b64decode(result["data"][0]["b64_json"], validate=True)
            if (image[:4] != b"RIFF" or image[8:12] != b"WEBP"
                    or int.from_bytes(image[4:8], "little") != len(image) - 8):
                raise ValueError("Invalid WebP image")
            return image
        except HTTPError as error:
            raise RecipeAIError("Image provider returned an HTTP error", reason="upstream_http",
                                upstream_status=error.code) from error
        except (TimeoutError, URLError, OSError) as error:
            detail = getattr(error, "reason", error)
            reason = "timeout" if isinstance(detail, TimeoutError) else "connection_failed"
            raise RecipeAIError("Image provider connection failed", reason=reason) from error
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise RecipeAIError("Image provider returned an invalid result") from error

    def generate_svg(self, name):
        for attempt in range(3):
            try:
                result = self.complete(
                    '请为指定食材画一张正方形缩略图。只返回 JSON 对象 {"svg":"完整 SVG"}。'
                    '使用 viewBox="0 0 128 128" 和 SVG 命名空间。画面居中，浅米色背景，'
                    '形状清晰，配色自然，适合 30px 缩略图。允许 svg 根元素、g、path、'
                    'rect、circle、ellipse、polygon、polyline 和 line。颜色用十六进制，'
                    '可用 opacity，坐标用纯数字。不要文字、渐变、transform、CSS、脚本或链接。',
                    {"ingredient": name}, timeout=60, json_mode=True,
                )
                return validate_ingredient_svg(result.get("svg") if isinstance(result, dict) else None)
            except RecipeAIError as error:
                if attempt == 0 and error.reason == "invalid_response":
                    continue
                if attempt < 2 and error.reason == "upstream_http" and error.upstream_status in (429, 504):
                    sleep(2 ** attempt)
                    continue
                raise


SVG_NS = "http://www.w3.org/2000/svg"
SVG_ATTRIBUTES = {
    "svg": {"viewBox", "width", "height"},
    "g": set(),
    "path": {"d"},
    "rect": {"x", "y", "width", "height", "rx", "ry"},
    "circle": {"cx", "cy", "r"},
    "ellipse": {"cx", "cy", "rx", "ry"},
    "polygon": {"points"},
    "polyline": {"points"},
    "line": {"x1", "y1", "x2", "y2"},
}
SVG_PAINT = {"fill", "stroke"}
SVG_NUMERIC_STYLE = {"opacity", "fill-opacity", "stroke-opacity", "stroke-width"}


def validate_ingredient_svg(svg):
    invalid = RecipeAIError("Image provider returned an unsupported SVG")
    if not isinstance(svg, str) or len(svg) > 32_768 or "<!" in svg:
        raise invalid
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as error:
        raise invalid from error
    if root.tag != f"{{{SVG_NS}}}svg" or root.get("viewBox") != "0 0 128 128":
        raise invalid

    count = 0

    def clean(node, depth=0):
        nonlocal count
        count += 1
        tag = node.tag.removeprefix(f"{{{SVG_NS}}}")
        if (count > 160 or depth > 10 or tag not in SVG_ATTRIBUTES
                or node.tag != f"{{{SVG_NS}}}{tag}" or (tag == "svg" and depth)
                or (node.text or "").strip() or (node.tail or "").strip()
                or (len(node) and tag not in ("svg", "g"))):
            raise invalid
        output = ET.Element(tag)
        for key, value in node.attrib.items():
            if key == "viewBox" and tag == "svg":
                output.set(key, value)
                continue
            if key in SVG_PAINT:
                pattern = r"(?:none|#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})"
            elif key == "d" and tag == "path":
                pattern = r"[MmLlHhVvCcSsQqTtAaZz0-9., +\-]+"
            elif key == "points" and tag in ("polygon", "polyline"):
                pattern = r"[0-9., +\-]+"
            elif key in SVG_NUMERIC_STYLE or key in SVG_ATTRIBUTES[tag]:
                pattern = r"-?(?:[0-9]{1,4}(?:\.[0-9]{1,5})?|\.[0-9]{1,5})"
            else:
                raise invalid
            if not re.fullmatch(pattern, value):
                raise invalid
            output.set(key, value)
        for child in node:
            output.append(clean(child, depth + 1))
        return output

    if not len(root):
        raise invalid
    output = clean(root)
    output.set("xmlns", SVG_NS)
    output.set("width", "128")
    output.set("height", "128")
    return ET.tostring(output, encoding="utf-8")
