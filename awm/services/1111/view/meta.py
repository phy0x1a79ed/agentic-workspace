"""Parse the generation parameters the webui embeds in each image it saves."""
import os
import re

from PIL import Image

# Same shape as the webui's own parser: `Key: value` pairs, comma separated,
# where a value may be a double-quoted string containing commas.
_PARAM_RE = re.compile(r'\s*(\w[\w \-/+.()]*):\s*("(?:\\.|[^\\"])*"|[^,]*)(?:,|$)')
_NETWORK_RE = re.compile(r"<(lora|lyco):([^:>]+)(?::([^:>]*))?[^>]*>")
_NEGATIVE = "Negative prompt:"


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _parse_param_line(line):
    params = {}
    for m in _PARAM_RE.finditer(line):
        params[m.group(1).strip()] = _unquote(m.group(2))
    return params


def _weight(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 1.0


def extract_loras(prompt, params):
    """LoRA/LyCORIS networks with weights, from prompt tags then the hashes field."""
    loras = []
    seen = set()
    for m in _NETWORK_RE.finditer(prompt or ""):
        name = m.group(2).strip()
        if name in seen:
            continue
        seen.add(name)
        loras.append({"name": name, "weight": _weight(m.group(3))})
    for entry in (params.get("Lora hashes") or "").split(","):
        name = entry.split(":", 1)[0].strip()
        if name and name not in seen:
            seen.add(name)
            loras.append({"name": name, "weight": None})
    return loras


def parse_parameters(text):
    """Split the webui's parameters text into prompt, negative prompt and fields."""
    lines = text.strip().replace("\r\n", "\n").split("\n")
    params = {}
    if lines and len(_PARAM_RE.findall(lines[-1])) >= 3:
        params = _parse_param_line(lines.pop())
    prompt_lines, negative_lines = [], []
    target = prompt_lines
    for line in lines:
        if target is prompt_lines and line.startswith(_NEGATIVE):
            target = negative_lines
            line = line[len(_NEGATIVE):].lstrip()
        target.append(line)
    prompt = "\n".join(prompt_lines).strip()
    return {
        "prompt": prompt,
        "negative": "\n".join(negative_lines).strip(),
        "params": params,
        "loras": extract_loras(prompt, params),
    }


def mode_for(relpath, params):
    top = relpath.split("/", 1)[0]
    if top.startswith("txt2img"):
        return "txt2img"
    if top.startswith("img2img"):
        return "img2img"
    # Hires fix also writes a denoising strength on txt2img output.
    if "Denoising strength" in params and "Hires upscale" not in params and "Hires upscaler" not in params:
        return "img2img"
    return "txt2img" if params else None


def _sidecar(path):
    txt = os.path.splitext(path)[0] + ".txt"
    return txt if os.path.isfile(txt) else None


def read_image(path, relpath):
    """Size and parsed parameters for one image file; never raises on a bad file."""
    result = {"width": None, "height": None, "meta": None, "error": None}
    text = None
    try:
        with Image.open(path) as im:
            result["width"], result["height"] = im.size
            text = im.info.get("parameters")
    except Exception as e:  # truncated or unreadable files still get indexed
        result["error"] = f"{type(e).__name__}: {e}"
    if text is None:
        sidecar = _sidecar(path)
        if sidecar:
            with open(sidecar, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
    if text:
        meta = parse_parameters(text)
        meta["mode"] = mode_for(relpath, meta["params"])
        result["meta"] = meta
    return result
