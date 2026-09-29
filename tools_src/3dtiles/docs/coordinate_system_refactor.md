# 坐标系重构方案

## 1. 背景

当前支持的源文件主要有：
- **Shapefile**: 坐标系依赖shp文件解析结果（地理坐标系和投影坐标系）
- **倾斜摄影(Smart3D目录格式)**: 坐标系依赖metadata.xml解析结果（ENU/EPSG/WKT）
- **单个OSGB文件**: 本地笛卡尔坐标系，无地理参考信息
- **FBX**: 本地笛卡尔坐标系，无地理参考信息

### 当前问题

`GeoTransform`类使用了大量静态inline变量作为共享状态：

```cpp
static inline double OriginX = 0.0;
static inline double OriginY = 0.0;
static inline double OriginZ = 0.0;
static inline double GeoOriginLon = 0.0;
static inline double GeoOriginLat = 0.0;
static inline double GeoOriginHeight = 0.0;
static inline bool IsENU = false;
static inline glm::dmat4 EcefToEnuMatrix = glm::dmat4(1);
// ...
```

**问题**：
- 多线程环境下存在竞态条件风险
- `pOgrCT`是thread_local，但其他变量不是，导致状态不一致
- 全局状态难以测试和维护

## 2. 命名空间规划

### 2.1 当前命名空间现状

现有代码大部分在全局命名空间，只有 `GeoidHeight` 使用了命名空间：

```cpp
// 现有命名空间
namespace GeoidHeight { ... }  // GeoidHeight.h/cpp
```

### 2.2 新增命名空间

新增的坐标系相关代码统一使用 `coords` 命名空间：

| 文件 | 命名空间 | 说明 |
|------|----------|------|
| `coordinate_system.h/cpp` | `coords` | 坐标系定义 |
| `coordinate_transformer.h/cpp` | `coords` | 坐标转换器 |

### 2.3 命名空间使用示例

```cpp
#include "coordinate_system.h"
#include "coordinate_transformer.h"

// 使用完整命名空间
coords::CoordinateSystem cs = coords::CoordinateSystem::LocalCartesian(coords::UpAxis::Z_UP);
coords::CoordinateTransformer transformer(cs);

// 或使用命名空间别名
namespace crd = coords;
crd::GeoReference geo_ref = crd::GeoReference::FromDegrees(120.0, 30.0, 0.0);
```

### 2.4 与现有代码的集成

```cpp
// 在需要使用新接口的文件中
#include "coordinate_transformer.h"
#include "GeoidHeight.h"

void ProcessTile() {
    // 新接口
    coords::CoordinateTransformer transformer(cs, geo_ref);

    // 现有接口（保持不变）
    GeoidHeight::GetGlobalGeoidCalculator().GetGeoidHeight(lat, lon);
}
```

## 3. 设计原则

### 3.1 坐标系与地理参考分离

**坐标系** 和 **地理参考** 是两个独立的概念：

| 概念 | 说明 |
|------|------|
| **坐标系** | 描述数据如何表示：本地笛卡尔、EPSG编码、WKT、ENU |
| **地理参考** | 描述如何将坐标映射到地球表面（可选） |

### 3.2 坐标系类型

| 类型 | 数据源 | 坐标系信息来源 | 特点 |
|------|--------|---------------|------|
| **LocalCartesian** | FBX, 单个OSGB | 无 | 本地笛卡尔坐标，无地理参考概念 |
| **ENU** | 倾斜摄影metadata.xml | 原点经纬度+偏移 | 自带地理参考 |
| **EPSG** | Shapefile, 倾斜摄影 | EPSG编码 | 通过OGR转换得到地理参考 |
| **WKT** | 倾斜摄影 | WKT字符串 | 通过OGR转换得到地理参考 |

### 3.3 轴方向

| 格式 | 上轴 | 手性 | 说明 |
|------|------|------|------|
| FBX | Y-Up | 右手 | Autodesk产品 |
| OSGB | Z-Up | 右手 | OpenSceneGraph |
| glTF/3D Tiles | Y-Up | 右手 | Cesium标准 |

### 3.4 椭球模型

不同地理坐标系使用不同的椭球模型，椭球参数直接影响地理坐标→ECEF的转换：

| 坐标系 | 椭球模型 | 长半轴 a (m) | 扁率 f |
|--------|----------|-------------|--------|
| EPSG:4326 (WGS84) | WGS84 | 6378137.0 | 1/298.257223563 |
| EPSG:4545 (CGCS2000) | CGCS2000 | 6378137.0 | 1/298.257222101 |
| EPSG:4214 (北京54) | 克拉索夫斯基 | 6378245.0 | 1/298.3 |
| EPSG:4610 (西安80) | IAG 1975 | 6378140.0 | 1/298.257 |

**处理方式**：
- OGR/GDAL在处理EPSG/WKT坐标系转换时，会自动使用正确的椭球参数
- ENU坐标系默认使用WGS84椭球（与3D Tiles标准一致）
- 本地笛卡尔坐标系不涉及椭球模型

### 3.5 高程基准

不同坐标系使用不同的高程基准，需要进行Geoid高度校正：

| 高程基准 | 基准面 | 说明 |
|----------|--------|------|
| WGS84椭球高 | WGS84椭球面 | GPS直接测量得到 |
| 中国1985国家高程 | 黄海平均海水面 | 中国常用正高系统 |
| EGM96/EGM2008 | 大地水准面 | 全球大地水准面模型 |

**高程关系**：
```
椭球高 (Ellipsoidal Height) = 正高 (Orthometric Height) + Geoid高度 (大地水准面差距)
```

**处理方式**：
- EPSG/WKT坐标系：源数据可能是正高，需要通过Geoid校正转换为椭球高
- ENU坐标系：假设为WGS84椭球高
- 本地笛卡尔坐标系：高度由用户指定，假设为椭球高

### 3.6 坐标转换链

完整的坐标转换链需要考虑椭球模型和高程基准：

```
源坐标系 (EPSG/WKT)          源坐标系 (ENU)           源坐标系 (LocalCartesian)
        │                           │                          │
        ▼                           │                          │
  ┌─────────────┐                   │                          │
  │ OGR坐标转换 │                   │                          │
  │ (自动处理   │                   │                          │
  │  椭球差异)  │                   │                          │
  └─────────────┘                   │                          │
        │                           │                          │
        ▼                           │                          │
  ┌─────────────┐                   │                          │
  │ Geoid高度校正│                  │                          │
  │ (正高→椭球高)│                  │                          │
  └─────────────┘                   │                          │
        │                           │                          │
        ▼                           ▼                          ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │                    WGS84 地理坐标 (经度, 纬度, 椭球高)           │
  └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │                    ECEF 地心地固坐标 (X, Y, Z)                   │
  │                    使用WGS84椭球参数计算                         │
  └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │                    ENU 局部切平面坐标 (东, 北, 天)               │
  │                    3D Tiles使用的坐标系统                        │
  └─────────────────────────────────────────────────────────────────┘
```

## 4. 架构设计

### 4.1 类图

```
┌─────────────────────────────────────────────────────────────────────┐
│                      CoordinateSystem (值类型)                       │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │ type: LocalCartesian | ENU | EPSG | WKT                       │ │
│  │ params: variant<LocalCartesianParams, ENUParams, ...>         │ │
│  └───────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ 创建
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   CoordinateTransformer (实例)                       │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │ mode: None | WithGeoReference                                  │ │
│  │ source_cs_: CoordinateSystem                                   │ │
│  │ geo_origin_*: 地理原点(WGS84)                                  │ │
│  │ enu_to_ecef_, ecef_to_enu_: 转换矩阵                           │ │
│  │ ogr_transform_: OGR转换器(可选)                                │ │
│  └───────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 转换流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CoordinateTransformer 内部流程                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  LocalCartesian (FBX/单个OSGB) + GeoReference:                      │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐                      │
│  │ 源坐标   │ -> │ 轴方向转换│ -> │ ECEF坐标 │ -> ENU局部坐标       │
│  │(笛卡尔)  │    │(可选)    │    │          │                       │
│  └──────────┘    └──────────┘    └──────────┘                      │
│                                                                     │
│  ENU (倾斜摄影):                                                    │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐                      │
│  │ 源坐标   │ -> │ +偏移量  │ -> │ ECEF坐标 │ -> ENU局部坐标       │
│  │(ENU米制) │    │(SRSOrigin)│   │          │                       │
│  └──────────┘    └──────────┘    └──────────┘                      │
│                                                                     │
│  EPSG/WKT (Shapefile/倾斜摄影):                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │ 源坐标   │ -> │ OGR转换  │ -> │ Geoid校正│ -> │ ECEF坐标 │ ->ENU │
│  │(投影坐标)│    │->WGS84   │    │(可选)    │    │          │       │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## 5. 详细设计

### 5.1 CoordinateSystem

```cpp
// src/coordinate_system.h
#pragma once

#include <string>
#include <variant>
#include <optional>
#include <glm/glm.hpp>

namespace coords {

enum class CoordinateType {
    Unknown,            // 未知/未初始化
    LocalCartesian,     // 本地笛卡尔坐标系(FBX, OSGB)
    ENU,                // ENU局部坐标系(倾斜摄影)
    EPSG,               // EPSG编码定义的坐标系
    WKT                 // WKT字符串定义的坐标系
};

enum class UpAxis {
    Y_UP,
    Z_UP
};

enum class Handedness {
    RightHanded,
    LeftHanded
};

// 高程基准类型
enum class VerticalDatum {
    Ellipsoidal,        // 椭球高（无需校正）
    Orthometric,        // 正高（需要Geoid校正）
    Unknown             // 未知（根据坐标系推断）
};

// 地理参考点（可选）
struct GeoReference {
    double lon;         // 经度(度)
    double lat;         // 纬度(度)
    double height;      // 高度(米)
    VerticalDatum datum = VerticalDatum::Ellipsoidal;  // 高程基准

    static GeoReference FromDegrees(double lon, double lat, double height,
                                    VerticalDatum datum = VerticalDatum::Ellipsoidal) {
        return {lon, lat, height, datum};
    }
};

// 本地笛卡尔坐标系参数
struct LocalCartesianParams {
    UpAxis up_axis = UpAxis::Y_UP;
    Handedness handedness = Handedness::RightHanded;

    static LocalCartesianParams YUp() {
        return {UpAxis::Y_UP, Handedness::RightHanded};
    }
    static LocalCartesianParams ZUp() {
        return {UpAxis::Z_UP, Handedness::RightHanded};
    }
};

// ENU坐标系参数
struct ENUParams {
    double origin_lon;      // 原点经度(度)
    double origin_lat;      // 原点纬度(度)
    double origin_height;   // 原点高度(米)
    double offset_x;        // SRSOrigin X偏移(米)
    double offset_y;        // SRSOrigin Y偏移(米)
    double offset_z;        // SRSOrigin Z偏移(米)

    GeoReference GetGeoReference() const {
        return {origin_lon, origin_lat, origin_height};
    }
};

// EPSG坐标系参数
struct EPSGParams {
    int code;               // EPSG编码
    double origin_x;        // 原点X
    double origin_y;        // 原点Y
    double origin_z;        // 原点Z
    VerticalDatum vertical_datum = VerticalDatum::Unknown;  // 高程基准
};

// WKT坐标系参数
struct WKTParams {
    std::string wkt;        // WKT字符串
    double origin_x;
    double origin_y;
    double origin_z;
    VerticalDatum vertical_datum = VerticalDatum::Unknown;  // 高程基准
};

class CoordinateSystem {
public:
    CoordinateSystem() = default;

    // ----- 工厂方法 -----

    // 本地笛卡尔坐标系（无地理参考概念）
    static CoordinateSystem LocalCartesian(UpAxis up_axis = UpAxis::Y_UP,
                                          Handedness handedness = Handedness::RightHanded);
    static CoordinateSystem LocalCartesian(const LocalCartesianParams& params);

    // ENU坐标系（自带地理参考）
    static CoordinateSystem ENU(double lon, double lat, double height,
                                double offset_x, double offset_y, double offset_z);

    // EPSG坐标系
    static CoordinateSystem EPSG(int code,
                                 double origin_x, double origin_y, double origin_z);

    // WKT坐标系
    static CoordinateSystem WKT(const std::string& wkt,
                                double origin_x, double origin_y, double origin_z);

    // ----- 查询方法 -----

    CoordinateType Type() const { return type_; }
    bool IsValid() const { return type_ != CoordinateType::Unknown; }

    bool NeedsOGRTransform() const;
    bool HasBuiltinGeoReference() const;

    std::optional<GeoReference> GetBuiltinGeoReference() const;
    std::tuple<double, double, double> GetSourceOrigin() const;

    std::optional<ENUParams> GetENUParams() const;
    std::optional<LocalCartesianParams> GetLocalCartesianParams() const;
    std::optional<int> GetEPSGCode() const;
    std::optional<std::string> GetWKTString() const;

    UpAxis GetUpAxis() const;
    Handedness GetHandedness() const;

    std::string ToString() const;

private:
    CoordinateType type_ = CoordinateType::Unknown;
    std::variant<std::monostate, LocalCartesianParams, ENUParams, EPSGParams, WKTParams> params_;
};

} // namespace coords
```

### 5.2 CoordinateTransformer

```cpp
// src/coordinate_transformer.h
#pragma once

#include "coordinate_system.h"
#include <ogr_spatialref.h>
#include <memory>
#include <glm/glm.hpp>

namespace coords {

// 转换模式
enum class TransformMode {
    None,               // 无地理转换（OSGB→GLTF场景）
    WithGeoReference    // 带地理参考转换（3D Tiles场景）
};

// Geoid配置
struct GeoidConfig {
    bool enabled = false;                // 是否启用Geoid校正
    GeoidHeight::GeoidModel model = GeoidHeight::GeoidModel::EGM96;  // Geoid模型
    std::string data_path;               // Geoid数据文件路径

    static GeoidConfig Disabled() { return {false}; }
    static GeoidConfig EGM96(const std::string& path = "") {
        return {true, GeoidHeight::GeoidModel::EGM96, path};
    }
    static GeoidConfig EGM2008(const std::string& path = "") {
        return {true, GeoidHeight::GeoidModel::EGM2008, path};
    }
};

class CoordinateTransformer {
public:
    // 模式1：无地理转换（纯格式转换，如OSGB→GLTF）
    explicit CoordinateTransformer(const CoordinateSystem& cs);

    // 模式2：带地理参考转换（3D Tiles场景）
    CoordinateTransformer(const CoordinateSystem& cs, const GeoReference& geo_ref);

    // 模式3：带地理参考和Geoid配置
    CoordinateTransformer(const CoordinateSystem& cs, const GeoReference& geo_ref,
                          const GeoidConfig& geoid_config);

    ~CoordinateTransformer();

    CoordinateTransformer(const CoordinateTransformer&) = delete;
    CoordinateTransformer& operator=(const CoordinateTransformer&) = delete;
    CoordinateTransformer(CoordinateTransformer&&) noexcept;
    CoordinateTransformer& operator=(CoordinateTransformer&&) noexcept;

    // ----- 模式查询 -----

    TransformMode GetMode() const { return mode_; }
    bool HasGeoReference() const { return mode_ == TransformMode::WithGeoReference; }

    // ----- 坐标转换（仅HasGeoReference时有效）-----

    glm::dvec3 ToWGS84(const glm::dvec3& point) const;
    glm::dvec3 ToECEF(const glm::dvec3& point) const;
    glm::dvec3 ToLocalENU(const glm::dvec3& point) const;

    void TransformToWGS84(std::vector<glm::dvec3>& points) const;
    void TransformToLocalENU(std::vector<glm::dvec3>& points) const;

    // ----- 轴方向转换（所有模式都可用）-----

    glm::dvec3 ConvertUpAxis(const glm::dvec3& point,
                             UpAxis target_axis = UpAxis::Y_UP) const;

    // ----- 矩阵访问 -----

    const glm::dmat4& GetEnuToEcefMatrix() const { return enu_to_ecef_; }
    const glm::dmat4& GetEcefToEnuMatrix() const { return ecef_to_enu_; }

    // ----- 原点信息 -----

    double GeoOriginLon() const { return geo_origin_lon_; }
    double GeoOriginLat() const { return geo_origin_lat_; }
    double GeoOriginHeight() const { return geo_origin_height_; }

    // ----- Geoid配置 -----

    void EnableGeoidCorrection(bool enabled) { geoid_config_.enabled = enabled; }
    bool IsGeoidCorrectionEnabled() const { return geoid_config_.enabled; }
    const GeoidConfig& GetGeoidConfig() const { return geoid_config_; }

    // ----- 静态工具方法 -----

    static glm::dmat4 CalcEnuToEcefMatrix(double lon_deg, double lat_deg, double height);
    static glm::dvec3 CartographicToEcef(double lon_deg, double lat_deg, double height);
    static glm::dmat4 GetAxisTransformMatrix(UpAxis from, UpAxis to);

private:
    void InitializeWithGeoRef(const GeoReference& geo_ref);
    void CreateOGRTransform();
    double ApplyGeoidCorrection(double lat, double lon, double height) const;
    bool ShouldApplyGeoidCorrection() const;

    CoordinateSystem source_cs_;
    TransformMode mode_ = TransformMode::None;

    double geo_origin_lon_ = 0.0;
    double geo_origin_lat_ = 0.0;
    double geo_origin_height_ = 0.0;

    glm::dmat4 enu_to_ecef_{1.0};
    glm::dmat4 ecef_to_enu_{1.0};
    glm::dmat4 axis_transform_{1.0};

    struct OGRCTDeleter;
    std::unique_ptr<OGRCoordinateTransformation, OGRCTDeleter> ogr_transform_;

    GeoidConfig geoid_config_;
};

} // namespace coords
```

## 6. 使用示例

### 6.1 场景1: OSGB → GLTF（无地理转换）

```cpp
auto cs = CoordinateSystem::LocalCartesian(UpAxis::Z_UP);
CoordinateTransformer transformer(cs);  // mode = None
// 只能使用轴方向转换
glm::dvec3 gltf_point = transformer.ConvertUpAxis(osgb_point, UpAxis::Y_UP);
```

### 5.2 场景2: OSGB → 3D Tiles（带地理参考）

```cpp
auto cs = CoordinateSystem::LocalCartesian(UpAxis::Z_UP);
GeoReference geo_ref = GeoReference::FromDegrees(120.0, 30.0, 0.0);
CoordinateTransformer transformer(cs, geo_ref);  // mode = WithGeoReference
// 可以使用完整转换
glm::dvec3 enu_point = transformer.ToLocalENU(osgb_point);
```

### 5.3 场景3: FBX → 3D Tiles

```cpp
auto cs = CoordinateSystem::LocalCartesian(UpAxis::Y_UP);
GeoReference geo_ref = GeoReference::FromDegrees(120.0, 30.0, 0.0);
CoordinateTransformer transformer(cs, geo_ref);
```

### 5.4 场景4: 倾斜摄影ENU（自带地理参考）

```cpp
// metadata.xml: <SRS>ENU:35.90924,117.13183</SRS>
//               <SRSOrigin>-958,-993,69</SRSOrigin>
auto cs = CoordinateSystem::ENU(117.13183, 35.90924, 0.0, -958.0, -993.0, 69.0);
CoordinateTransformer transformer(cs);  // 自动使用内置geo_ref
```

### 6.5 场景5: 倾斜摄影EPSG（OGR计算geo_ref）

```cpp
auto cs = CoordinateSystem::EPSG(4547, 500000.0, 3000000.0, 0.0);
GeoReference placeholder{0, 0, 0};  // 会被OGR转换结果覆盖
CoordinateTransformer transformer(cs, placeholder);
```

## 6. 各数据源的处理流程

| 数据源 | CoordinateSystem | GeoReference来源 | 说明 |
|--------|-----------------|------------------|------|
| OSGB→GLTF | LocalCartesian(Z-Up) | **无** | 纯格式转换 |
| OSGB→3DTiles | LocalCartesian(Z-Up) | 命令行参数 | 需要地理定位 |
| FBX→3DTiles | LocalCartesian(Y-Up) | 命令行参数 | 需要地理定位 |
| 倾斜摄影(ENU) | ENU | metadata.xml 内置 | 自带地理参考 |
| 倾斜摄影(EPSG) | EPSG | OGR转换计算 | 需要GDAL |
| 倾斜摄影(WKT) | WKT | OGR转换计算 | 需要GDAL |
| Shapefile | EPSG/WKT | OGR转换计算 | 从.prj解析 |

## 8. 重构步骤

| 步骤 | 内容 | 影响文件 |
|------|------|----------|
| 1 | 新增 `coordinate_system.h/cpp` | 新文件 |
| 2 | 新增 `coordinate_transformer.h/cpp` | 新文件 |
| 3 | 添加单元测试 | tests/test_coordinate_system.cpp |
| 4 | 重构 `tileset.cpp` 使用新接口 | tileset.cpp |
| 5 | 重构 `osgb23dtile.cpp` 使用新接口 | osgb23dtile.cpp |
| 6 | 重构 `shp23dtile.cpp` 使用新接口 | shp23dtile.cpp |
| 7 | 重构 `FBXPipeline.cpp` 使用新接口 | FBXPipeline.cpp |
| 8 | 重构 Rust FFI 层 | src/fun_c.rs, src/osgb.rs |
| 9 | 更新 `main.rs` 传递 CoordinateSystem | main.rs |
| 10 | 删除 `GeoTransform.h/cpp` | 移除旧文件 |

## 8. 兼容性保证

- 直接使用 `CoordinateTransformer` 替换 `GeoTransform`，不保留适配层
- 逐步迁移，确保每一步都可编译运行
- 保留所有extern "C"接口不变

## 10. Geoid高度校正

### 10.1 高程基准判断逻辑

```cpp
bool CoordinateTransformer::ShouldApplyGeoidCorrection() const {
    // 1. Geoid配置必须启用
    if (!geoid_config_.enabled) return false;

    // 2. Geoid计算器必须已初始化
    if (!GeoidHeight::GetGlobalGeoidCalculator().IsInitialized()) return false;

    // 3. 根据坐标系类型和垂直基准判断
    switch (source_cs_.Type()) {
        case CoordinateType::EPSG:
        case CoordinateType::WKT: {
            // EPSG/WKT坐标系：检查垂直基准
            auto datum = source_cs_.GetVerticalDatum();
            return datum == VerticalDatum::Orthometric || datum == VerticalDatum::Unknown;
        }
        case CoordinateType::ENU:
            // ENU坐标系：假设为WGS84椭球高，不需要校正
            return false;
        case CoordinateType::LocalCartesian:
            // 本地笛卡尔：用户指定的高度假设为椭球高
            return false;
        default:
            return false;
    }
}
```

### 10.2 使用示例

```cpp
// 场景1: EPSG坐标系 + 中国1985高程基准（需要Geoid校正）
auto cs = CoordinateSystem::EPSG(4545, 500000.0, 3000000.0, 0.0);
cs.SetVerticalDatum(VerticalDatum::Orthometric);  // 设置为正高
GeoReference geo_ref = GeoReference::FromDegrees(0, 0, 0);
GeoidConfig geoid_cfg = GeoidConfig::EGM96("/path/to/geoid.pgm");
CoordinateTransformer transformer(cs, geo_ref, geoid_cfg);

// 场景2: EPSG坐标系 + WGS84椭球高（不需要Geoid校正）
auto cs = CoordinateSystem::EPSG(4326, 117.0, 30.0, 0.0);
GeoReference geo_ref = GeoReference::FromDegrees(0, 0, 0, VerticalDatum::Ellipsoidal);
CoordinateTransformer transformer(cs, geo_ref);  // 不传GeoidConfig

// 场景3: ENU坐标系（默认椭球高，不需要校正）
auto cs = CoordinateSystem::ENU(117.0, 30.0, 0.0, 0, 0, 0);
CoordinateTransformer transformer(cs);

// 场景4: 本地笛卡尔 + 用户指定椭球高
auto cs = CoordinateSystem::LocalCartesian(UpAxis::Z_UP);
GeoReference geo_ref = GeoReference::FromDegrees(120.0, 30.0, 100.0);
CoordinateTransformer transformer(cs, geo_ref);
```

### 10.3 内部实现

```cpp
double CoordinateTransformer::ApplyGeoidCorrection(double lat, double lon, double height) const {
    if (!ShouldApplyGeoidCorrection()) return height;

    // 正高 → 椭球高
    return GeoidHeight::GetGlobalGeoidCalculator()
        .ConvertOrthometricToEllipsoidal(lat, lon, height);
}
```
