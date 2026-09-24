#include <cstdio>
#include <cmath>
#include <cassert>
#include "coordinate_system.h"
#include "coordinate_transformer.h"

namespace coords {
namespace test {

constexpr double PI = 3.14159265358979323846;
constexpr double EPSILON = 1e-6;

void test_local_cartesian_creation() {
    printf("[Test] LocalCartesian creation...\n");

    auto cs = CoordinateSystem::LocalCartesian(UpAxis::Y_UP);
    assert(cs.Type() == CoordinateType::LocalCartesian);
    assert(cs.IsValid());
    assert(cs.GetUpAxis() == UpAxis::Y_UP);
    assert(cs.GetHandedness() == Handedness::RightHanded);
    assert(!cs.NeedsOGRTransform());
    assert(!cs.HasBuiltinGeoReference());

    auto cs_zup = CoordinateSystem::LocalCartesian(UpAxis::Z_UP);
    assert(cs_zup.GetUpAxis() == UpAxis::Z_UP);

    printf("[Test] LocalCartesian creation: PASSED\n");
}

void test_enu_creation() {
    printf("[Test] ENU creation...\n");

    auto cs = CoordinateSystem::ENU(117.0, 35.0, 0.0, -958.0, -993.0, 69.0);
    assert(cs.Type() == CoordinateType::ENU);
    assert(cs.IsValid());
    assert(!cs.NeedsOGRTransform());
    assert(cs.HasBuiltinGeoReference());

    auto geo_ref = cs.GetBuiltinGeoReference();
    assert(geo_ref.has_value());
    assert(std::abs(geo_ref->lon - 117.0) < EPSILON);
    assert(std::abs(geo_ref->lat - 35.0) < EPSILON);

    auto params = cs.GetENUParams();
    assert(params.has_value());
    assert(std::abs(params->offset_x - (-958.0)) < EPSILON);

    printf("[Test] ENU creation: PASSED\n");
}

void test_epsg_creation() {
    printf("[Test] EPSG creation...\n");

    auto cs = CoordinateSystem::EPSG(4326, 117.0, 35.0, 0.0);
    assert(cs.Type() == CoordinateType::EPSG);
    assert(cs.IsValid());
    assert(cs.NeedsOGRTransform());
    assert(!cs.HasBuiltinGeoReference());

    auto code = cs.GetEPSGCode();
    assert(code.has_value());
    assert(*code == 4326);

    auto [ox, oy, oz] = cs.GetSourceOrigin();
    assert(std::abs(ox - 117.0) < EPSILON);

    printf("[Test] EPSG creation: PASSED\n");
}

void test_wkt_creation() {
    printf("[Test] WKT creation...\n");

    std::string wkt = "GEOGCS[\"WGS 84\",DATUM[\"WGS_1984\",SPHEROID[\"WGS 84\",6378137,298.257223563]],PRIMEM[\"Greenwich\",0],UNIT[\"degree\",0.0174532925199433]]";
    auto cs = CoordinateSystem::WKT(wkt, 117.0, 35.0, 0.0);
    assert(cs.Type() == CoordinateType::WKT);
    assert(cs.IsValid());
    assert(cs.NeedsOGRTransform());

    auto wkt_str = cs.GetWKTString();
    assert(wkt_str.has_value());
    assert(wkt_str->find("WGS 84") != std::string::npos);

    printf("[Test] WKT creation: PASSED\n");
}

void test_vertical_datum() {
    printf("[Test] VerticalDatum...\n");

    auto cs = CoordinateSystem::EPSG(4545, 500000.0, 3000000.0, 0.0, VerticalDatum::Orthometric);
    assert(cs.GetVerticalDatum() == VerticalDatum::Orthometric);

    cs.SetVerticalDatum(VerticalDatum::Ellipsoidal);
    assert(cs.GetVerticalDatum() == VerticalDatum::Ellipsoidal);

    auto cs_enu = CoordinateSystem::ENU(117.0, 35.0, 0.0, 0, 0, 0);
    assert(cs_enu.GetVerticalDatum() == VerticalDatum::Ellipsoidal);

    printf("[Test] VerticalDatum: PASSED\n");
}

void test_axis_transform() {
    printf("[Test] Axis transform...\n");

    auto cs_zup = CoordinateSystem::LocalCartesian(UpAxis::Z_UP);
    CoordinateTransformer transformer(cs_zup);

    glm::dvec3 zup_point{1.0, 2.0, 3.0};  // x=1, y=2, z=3 (Z-Up)
    glm::dvec3 yup_point = transformer.ConvertUpAxis(zup_point, UpAxis::Y_UP);

    // Z-Up → Y-Up: (x, y, z) → (x, z, -y)
    assert(std::abs(yup_point.x - 1.0) < EPSILON);
    assert(std::abs(yup_point.y - 3.0) < EPSILON);  // z → y
    assert(std::abs(yup_point.z - (-2.0)) < EPSILON);  // -y → z

    printf("[Test] Axis transform: PASSED\n");
}

void test_cartographic_to_ecef() {
    printf("[Test] Cartographic to ECEF...\n");

    // 测试赤道上经度0度、纬度0度、高度0的点
    glm::dvec3 ecef = CoordinateTransformer::CartographicToEcef(0.0, 0.0, 0.0);

    // 赤道上，ECEF的X应该接近长半轴a
    constexpr double WGS84_A = 6378137.0;
    assert(std::abs(ecef.x - WGS84_A) < 1.0);  // X ≈ a
    assert(std::abs(ecef.y) < EPSILON);         // Y ≈ 0
    assert(std::abs(ecef.z) < EPSILON);         // Z ≈ 0

    printf("[Test] Cartographic to ECEF: PASSED\n");
}

void test_enu_to_ecef_matrix() {
    printf("[Test] ENU to ECEF matrix...\n");

    // 测试赤道上经度0度、纬度0度、高度0的ENU矩阵
    glm::dmat4 matrix = CoordinateTransformer::CalcEnuToEcefMatrix(0.0, 0.0, 0.0);

    // 检查平移部分（应该是赤道上点的ECEF坐标）
    constexpr double WGS84_A = 6378137.0;
    assert(std::abs(matrix[3].x - WGS84_A) < 1.0);  // X ≈ a

    // 检查旋转部分
    // 东(E)方向在赤道经度0处应该是Y方向
    assert(std::abs(matrix[0].x) < EPSILON);  // E_x ≈ 0
    assert(std::abs(matrix[0].y - 1.0) < EPSILON);  // E_y ≈ 1

    printf("[Test] ENU to ECEF matrix: PASSED\n");
}

void test_to_string() {
    printf("[Test] ToString...\n");

    auto cs_local = CoordinateSystem::LocalCartesian(UpAxis::Z_UP);
    std::string str = cs_local.ToString();
    assert(str.find("LocalCartesian") != std::string::npos);
    assert(str.find("Z_UP") != std::string::npos);

    auto cs_epsg = CoordinateSystem::EPSG(4326, 0, 0, 0);
    str = cs_epsg.ToString();
    assert(str.find("EPSG:4326") != std::string::npos);

    printf("[Test] ToString: PASSED\n");
}

void test_geo_reference() {
    printf("[Test] GeoReference...\n");

    auto geo_ref = GeoReference::FromDegrees(120.0, 30.0, 100.0, VerticalDatum::Ellipsoidal);
    assert(std::abs(geo_ref.lon - 120.0) < EPSILON);
    assert(std::abs(geo_ref.lat - 30.0) < EPSILON);
    assert(std::abs(geo_ref.height - 100.0) < EPSILON);
    assert(geo_ref.datum == VerticalDatum::Ellipsoidal);

    printf("[Test] GeoReference: PASSED\n");
}

void test_geoid_config() {
    printf("[Test] GeoidConfig...\n");

    auto disabled = GeoidConfig::Disabled();
    assert(!disabled.enabled);

    auto egm96 = GeoidConfig::EGM96("/path/to/geoid.pgm");
    assert(egm96.enabled);
    assert(egm96.model == GeoidHeight::GeoidModel::EGM96);

    auto egm2008 = GeoidConfig::EGM2008();
    assert(egm2008.enabled);
    assert(egm2008.model == GeoidHeight::GeoidModel::EGM2008);

    printf("[Test] GeoidConfig: PASSED\n");
}

int run_all_tests() {
    printf("========================================\n");
    printf("Coordinate System Unit Tests\n");
    printf("========================================\n\n");

    test_local_cartesian_creation();
    test_enu_creation();
    test_epsg_creation();
    test_wkt_creation();
    test_vertical_datum();
    test_axis_transform();
    test_cartographic_to_ecef();
    test_enu_to_ecef_matrix();
    test_to_string();
    test_geo_reference();
    test_geoid_config();

    printf("\n========================================\n");
    printf("All tests PASSED!\n");
    printf("========================================\n");

    return 0;
}

} // namespace test
} // namespace coords

int main() {
    return coords::test::run_all_tests();
}
