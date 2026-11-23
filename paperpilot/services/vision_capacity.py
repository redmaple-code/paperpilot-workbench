"""Conservative, source-declared image bounds, never a universal vision tokenizer."""
from __future__ import annotations

from io import BytesIO
import hashlib
import json

from PIL import Image, UnidentifiedImageError

from paperpilot.services.project_errors import ProjectServiceError

OUTPUT_TOKENS = 1600


def instruction(checks):
    return ("Review the supplied academic figure(s). The first image is the target; a second image is the located reference region. "
        "Do not invent evidence, measurements or scientific validity. Return only JSON with findings: [{check, status, message, location, limitations}]. "
        "status must be warning, failed, unknown or needs_human_review; never pass. Use only these requested check names: " + ",".join(checks))


def admit(admission, images, checks):
    """Count every rendered image plus serialized text/framing, output and margin.

    Caller-declared per-image bounds apply only at high detail and within the
    declared dimensions. They are unverified and must have a model-specific
    source; small PNG byte size does not imply a small vision token count.
    """
    from paperpilot.services.provider_runtime import admit_tokens

    model = admission['model']
    bounds = admission.get('model_limits', {}).get(model, {})
    vision = bounds.get('vision')
    if not vision:
        raise ProjectServiceError(409, 'vision_capacity_unknown',
            '视觉图片容量未知，未发送。打开“补齐视觉模型容量”，按所选模型high detail文档填写单图token保守上界、适用宽高和来源，保存、测试、启用并绑定视觉用途后重新预览；也可关闭云端，仅做本地核验。')
    if not images or len(images) > 2:
        raise ProjectServiceError(422, 'invalid_vision_images', '视觉核验需要一张待检图，最多加一张参考区域；未发送。')
    details = []
    for picture in images:
        try:
            with Image.open(BytesIO(picture)) as image:
                width, height = image.size
                if image.format != 'PNG' or width > 1600 or height > 1600:
                    raise ValueError('unsupported image')
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ProjectServiceError(422, 'invalid_vision_images', '视觉输入须为不超过1600像素的受管PNG；检查图像后重新预览，未发送。') from exc
        if width > vision['max_width'] or height > vision['max_height']:
            raise ProjectServiceError(422, 'vision_dimensions_exceeded',
                f"本次图像{width}×{height}超出已声明视觉容量适用尺寸{vision['max_width']}×{vision['max_height']}。请显式裁剪/导入缩小后的图像，或依据模型文档补齐较大尺寸的保守上界并重新测试绑定；未静默丢图，未发送。")
        details.append({'width': width, 'height': height, 'sha256': hashlib.sha256(picture).hexdigest(),
            'estimated_tokens': vision['tokens_per_image']})
    # Data URLs are represented by the model's image encoding, not tokenized as
    # literal base64 text. All other JSON/text and protocol framing are included.
    framing = {'model': model, 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': instruction(checks)},
        *[{'type': 'image_url', 'image_url': {'url': '', 'detail': 'high'}} for _ in images]]}],
        'max_tokens': OUTPUT_TOKENS, 'temperature': 0}
    text_tokens = len(json.dumps(framing, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 256
    image_tokens = sum(item['estimated_tokens'] for item in details)
    try:
        capacity = admit_tokens(admission, model, text_tokens + image_tokens, OUTPUT_TOKENS)
    except ProjectServiceError as exc:
        if exc.code == 'model_capacity_exceeded':
            raise ProjectServiceError(422, 'vision_capacity_exceeded',
                '完整视觉请求（全部图片、文本封装、1600输出预留及安全余量）超过模型上限，未发送。请明确减少参考图/缩小输入并核对其适用上界，或配置容量更大的视觉模型后重新预览；不会静默丢图或换模型。') from exc
        raise
    return {**capacity, 'estimated_text_and_framing_tokens': text_tokens, 'estimated_image_tokens': image_tokens,
        'image_count': len(details), 'images': details, 'detail': 'high', 'vision_capacity_source': vision['source'],
        'estimation_method': 'source_declared_per_image_upper_bound_plus_utf8_framing',
        'total_with_output_and_margin': capacity['estimated_input_tokens'] + OUTPUT_TOKENS + capacity['safety_margin_tokens']}
