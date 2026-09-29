# 关于本目录的入库范围

本目录是 [fanvanzh/3dtiles](https://github.com/fanvanzh/3dtiles) 的源码副本，
作为天地图下载处理工具的子工程维护（见项目 CLAUDE.md「三维处理管线」一节）。

## 入库的内容

即**我们实际修改过的部分**与构建所需的源文件：

| 路径 | 说明 |
|---|---|
| `src/` | 核心源码。本次修改涉及 `main.rs`、`osgb.rs`、`osgb23dtile.cpp` |
| `build.rs` | 构建脚本，本次有修改 |
| `Cargo.toml` / `Cargo.lock` | Rust 依赖清单 |
| `vcpkg-overlays/tinygltf/` | 新增的 vcpkg 覆盖端口 |
| `vcpkg-configuration.json` | vcpkg 配置，本次有修改 |
| `CMakeLists.txt`、`docs/`、`tests/` 等 | 上游源文件，原样保留 |

相对上游 `origin/master` 的改动共 8 个文件、约 1632 行新增（可用 git 备份包查看）。

## 未入库的内容及原因

| 路径 | 体积 | 原因 |
|---|---|---|
| `thirdparty/` | 64 MB | 上游的 git submodule（ufbx、base64），含自带测试数据 52 MB。非本项目改动，且入库会使仓库显著膨胀 |
| `geoids/` | 18 MB | 大地水准面数据 `egm96-5.pgm`，二进制数据文件 |
| `target/` | 157 MB | cargo 编译产物 |
| `vcpkg_installed/` | 894 MB | vcpkg 依赖构建产物 |

以上均已在主仓库 `.gitignore` 中排除。**重新构建前需自行补齐**：

```bash
# 1. 取回第三方子模块
git clone https://github.com/ufbx/ufbx.git thirdparty/ufbx
git clone https://github.com/tobiaslocker/base64.git thirdparty/base64

# 2. 取回大地水准面数据（用于 --geoid egm96 高程改正）
#    见上游仓库 geoids/ 目录

# 3. 构建
cargo build --release
```

## 编译产物

编译出的 `3dtile.exe` 与配套 DLL 放在项目根的 `tools/3dtiles/`（同样不入库，见主仓库 .gitignore）。
