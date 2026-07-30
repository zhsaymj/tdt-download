"""抓取 DataV 行政区索引(省→市→县三级 adcode/name/level 树)。

数据源:https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json
  - {adcode}_full.json 返回该区域下所有直接子级行政区(带边界坐标)
  - 我们只抽取 adcode/name/level 构建层级树,不存坐标(边界前端按需请求)

生成:frontendvue/public/area-index.json
结构:[{adcode,name,level,children:[{adcode,name,level,children:[...]}]}]

用法:python scripts/fetch_area_index.py
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

BASE = "https://geo.datav.aliyun.com/areas_v3/bound/{}_full.json"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "frontendvue" / "public" / "area-index.json"

# 直辖市/特别行政区市级与省级 adcode 相同,避免重复下钻
_session_headers = {"User-Agent": "Mozilla/5.0 (AreaIndexFetcher)"}


def fetch_children(adcode: str) -> list[dict]:
    """请求 {adcode}_full.json,返回其直接子级的精简节点列表。"""
    url = BASE.format(adcode)
    req = urllib.request.Request(url, headers=_session_headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        code = str(p.get("adcode", ""))
        name = p.get("name", "")
        level = p.get("level", "")
        if not code or not name:
            continue
        out.append({
            "adcode": code, "name": name, "level": level,
            "childrenNum": p.get("childrenNum", 0),
        })
    return out


def main():
    print("抓取全国省级列表…")
    provinces = fetch_children("100000")
    print(f"  省级 {len(provinces)} 个")

    tree = []
    for i, prov in enumerate(provinces, 1):
        node = {"adcode": prov["adcode"], "name": prov["name"],
                "level": "province", "children": []}
        print(f"[{i}/{len(provinces)}] {prov['name']} ({prov['adcode']}) 抓取市级…")
        try:
            cities = fetch_children(prov["adcode"])
        except Exception as e:
            print(f"  省 {prov['name']} 失败:{e}")
            tree.append(node)
            continue
        time.sleep(0.15)

        for city in cities:
            city_node = {"adcode": city["adcode"], "name": city["name"],
                         "level": "city", "children": []}
            # 有下级(县/区)才继续下钻;childrenNum=0 的市不请求
            if city.get("childrenNum"):
                try:
                    districts = fetch_children(city["adcode"])
                    for d in districts:
                        city_node["children"].append({
                            "adcode": d["adcode"], "name": d["name"], "level": "district",
                        })
                    time.sleep(0.12)
                except Exception as e:
                    print(f"    市 {city['name']} 下钻失败:{e}")
            node["children"].append(city_node)
        tree.append(node)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(tree, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"完成:{OUT} ({size_kb:.0f} KB),省 {len(tree)} 个")


if __name__ == "__main__":
    main()
