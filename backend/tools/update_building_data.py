"""重新生成建筑网格数据并打包,供手动上传到对象存储。

由 update-building-data.bat 调用,是"准备数据"的运维动作,不在界面暴露——
它要下载 1.5GB pbf、跑约半小时,普通使用者不该看到这个按钮。

流程:
  1. 取 pbf(本地已有则复用,--redownload 强制重新下载)
  2. 扫描全国建筑轮廓写入 0.05° 网格缓存(data/buildings/osm/)
  3. 打包成 0.5° 数据包(data/buildings/dist/osm/),供上传

用法(见 bat 的注释):
  python -m backend.tools.update_building_data              # 完整流程
  python -m backend.tools.update_building_data --redownload # 强制重下 pbf
  python -m backend.tools.update_building_data --pack-only  # 只重新打包
  python -m backend.tools.update_building_data --bbox w,s,e,n  # 只导入某范围
"""
from __future__ import annotations

import argparse
import sys
import time


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="update_building_data",
        description="重新生成建筑网格数据并打包,供上传到对象存储")
    parser.add_argument("--pbf", default="",
                        help="pbf 文件路径(默认取 config.yaml 的 buildings.pbf_path)")
    parser.add_argument("--pack-only", action="store_true",
                        help="跳过导入,只把现有网格缓存重新打包")
    parser.add_argument("--import-only", action="store_true",
                        help="只导入,不打包")
    parser.add_argument("--bbox", default="",
                        help="只导入该范围,格式 west,south,east,north(默认全国)")
    parser.add_argument("--source", default="osm", help="数据源目录名(默认 osm)")
    args = parser.parse_args(argv)

    bbox = None
    if args.bbox:
        try:
            parts = [float(x) for x in args.bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            bbox = tuple(parts)
        except ValueError:
            print("--bbox 格式错误,应为 west,south,east,north", file=sys.stderr)
            return 2

    t0 = time.time()

    # ---- 1/2 导入 ----
    if not args.pack_only:
        from ..core.pbf_import import import_from_pbf, resolve_pbf
        # 先确认 pbf 可用再打印计划,免得等半天才报"文件不存在"
        try:
            pbf = resolve_pbf(args.pbf or None)
        except RuntimeError as e:
            print(f"\n{e}", file=sys.stderr)
            return 1
        print("=" * 60)
        print("步骤 1/2:导入建筑轮廓到网格缓存")
        print(f"  pbf 文件:{pbf}({_fmt(pbf.stat().st_size)})")
        print("  范围:", args.bbox or "全国")
        print("  预计耗时约半小时(全国),请勿关闭窗口")
        print("=" * 60)
        try:
            st = import_from_pbf(bbox, pbf_path=pbf,
                                 cache_key=args.source, ttl_days=0)
        except Exception as e:
            print(f"\n导入失败:{e}", file=sys.stderr)
            return 1
        if st.get("interrupted"):
            print("\n导入被中断,未完成", file=sys.stderr)
            return 1
        print(f"\n导入完成:{st['buildings']} 栋建筑 → {st['cells']} 格,"
              f"扫描 {st['scan_seconds']:.0f}s / 写入 {st['group_seconds']:.0f}s")
        if st.get("coverage_bbox"):
            print(f"  覆盖范围:{st['coverage_bbox']}")

    # ---- 2/2 打包 ----
    if not args.import_only:
        from ..core.bundle_pack import pack
        print()
        print("=" * 60)
        print("步骤 2/2:打包成数据包(供上传)")
        print("=" * 60)
        try:
            r = pack(args.source)
        except Exception as e:
            print(f"\n打包失败:{e}", file=sys.stderr)
            return 1
        if r.get("interrupted"):
            print("\n打包被中断,未完成", file=sys.stderr)
            return 1
        print(f"\n打包完成:{r['bundle_count']} 个包 / "
              f"{r['total_features']} 栋 / {_fmt(r['total_bytes'])}")
        print(f"  版本号:{r['version']}")
        print(f"  覆盖范围:{r['coverage_bbox']}")
        print(f"  产物目录:{r['dir']}")
        print()
        print("-" * 60)
        print("接下来手动上传:")
        print(f"  把上面目录整个上传到对象存储,保留 {args.source}/ 这一层目录。")
        print("  上传后各机 config.yaml 的 buildings.remote_url 指向其父目录即可,")
        print("  例如 remote_url 为 https://.../building-china 时,")
        print(f"  实际会请求 https://.../building-china/{args.source}/index.json")
        print()
        print("  各机最多 6 小时会自动发现新版本;要立即生效,删除各机的")
        print(f"  data/buildings/{args.source}/_remote_index.json 即可。")
        print("-" * 60)

    print(f"\n全部完成,总用时 {time.time() - t0:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
