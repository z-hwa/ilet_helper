"""解析 Gemini 回傳的 JSON：比 json.loads 寬容一點。

Gemini 偶爾會在最後一個欄位後面多給一個逗號，或把整段包在 ``` 圍欄裡；嚴格解析會直接丟
「Expecting property name enclosed in double quotes」這類錯誤。這裡先修掉這些常見瑕疵再解析。
"""
import json
import re


def loads_lenient(text):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_error:
        # 物件或陣列結尾前多餘的逗號：{"a": 1,} / [1, 2,]
        fixed = re.sub(r",(\s*[}\]])", r"\1", raw)
        if fixed != raw:
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
        raise first_error
