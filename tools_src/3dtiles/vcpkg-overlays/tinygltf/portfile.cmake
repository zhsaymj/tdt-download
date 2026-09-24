# Header-only library
# 覆盖端口：上游 tinygltf v2.9.7 归档被 GitHub 重新生成导致 SHA512 变化，
# 官方端口内嵌旧哈希无法通过校验。此处更新为重新生成后的实际哈希。
vcpkg_from_github(
    OUT_SOURCE_PATH SOURCE_PATH
    REPO syoyo/tinygltf
    REF "v${VERSION}"
    SHA512 553c7ad329da5a4d46235747db9d937957d5698e74c8d1751c17da5a1d09d35f4212e2476652a63ee1a12ff74220531b5e288eaaddb1014d47000a82d30f03a2
    HEAD_REF master
)

# Put the licence file where vcpkg expects it
# Copy the tinygltf header files and fix the path to json
vcpkg_replace_string("${SOURCE_PATH}/tiny_gltf.h" "#include \"json.hpp\"" "#include <nlohmann/json.hpp>")
file(INSTALL "${SOURCE_PATH}/tiny_gltf.h" DESTINATION "${CURRENT_PACKAGES_DIR}/include")

vcpkg_install_copyright(FILE_LIST "${SOURCE_PATH}/LICENSE")
