extern crate libc;
extern crate rayon;
extern crate serde;
extern crate serde_json;

use std::fs;

use rayon::prelude::*;

use std::error::Error;
use std::path::Path;

use crate::common::str_to_vec_c;

extern "C" {

    fn osgb23dtile_path(
        in_path: *const u8,
        out_path: *const u8,
        box_ptr: *mut f64,
        len: *mut i32,
        x: f64,
        y: f64,
        max_lvl: i32,
        enable_texture_compress: bool,
        enable_meshopt: bool,
        enable_draco: bool,
        enable_unlit: bool,
    ) -> *mut libc::c_void;

    pub fn osgb2glb(name_in: *const u8, name_out: *const u8) -> bool;

	fn transform_c(radian_x: f64, radian_y: f64, height_min: f64, ptr: *mut f64);

	fn transform_c_with_enu_offset(center_x: f64, center_y: f64, height_min: f64,
	                               enu_offset_x: f64, enu_offset_y: f64, enu_offset_z: f64,
	                               ptr: *mut f64);

    pub fn epsg_convert(insrs: i32, val: *mut f64, gdal: *const libc::c_char, proj: *const libc::c_char) -> bool;

    pub fn enu_init(lon: f64, lat: f64, origin_enu: *mut f64, gdal: *const libc::c_char, proj: *const libc::c_char) -> bool;

    pub fn wkt_convert(gdal: *const libc::c_char, val: *mut f64, gdal: *const libc::c_char) -> bool;

    fn degree2rad(val: f64) -> f64;

    #[allow(dead_code)]
    fn meter_to_lati(m: f64) -> f64;

    #[allow(dead_code)]
    fn meter_to_longti(m: f64, lati: f64) -> f64;

    pub fn get_geo_origin_height() -> f64;

    fn build_top_pyramid(
        spec_json: *const u8,
        spec_len: i32,
        out_len: *mut i32,
    ) -> *mut libc::c_void;

}

#[derive(Debug)]
struct TileResult {
    json: String,
    path: String,
    in_dir: String,
    box_v: Vec<f64>,
}

struct OsgbInfo {
    in_dir: String,
    out_dir: String,
    sender: ::std::sync::mpsc::Sender<TileResult>,
}

pub fn osgb_batch_convert(
    dir: &Path,
    dir_dest: &Path,
    max_lvl: Option<i32>,
    center_x: f64,
    center_y: f64,
    region_offset: Option<f64>,
    enu_offset: Option<(f64, f64, f64)>,
    origin_height: Option<f64>,
    enable_texture_compress: bool,
    enable_meshopt: bool,
    enable_draco_compress: bool,
    enable_unlit: bool,
    enable_pyramid: bool,
) -> Result<(), Box<dyn Error>> {
    use std::fs::File;
    use std::io::prelude::*;
    use std::sync::mpsc::channel;

    let path = dir.join("Data");
    if !path.exists() || !path.is_dir() {
        return Err(From::from(format!("dir {} not exist", path.display())));
    }

    let (sender, receiver) = channel();
    let mut osgb_dir_pair: Vec<OsgbInfo> = vec![];
    let mut task_count = 0;
    fs::create_dir_all(dir_dest)?;
    for entry in fs::read_dir(&path)? {
        let entry = entry?;
        let path_tile = entry.path();
        if path_tile.is_dir() {
            // if Tile_xx_xx.osgb
            let stem = path_tile.file_stem().unwrap().to_str().unwrap();
            let osgb = path_tile.join(stem).with_extension("osgb");
            if osgb.exists() && !osgb.is_dir() {
                // convert this path
                task_count += 1;
                //let in_buf = str_to_vec_c(osgb.to_str().unwrap());
                let out_dir = dir_dest.join("Data").join(stem);
                fs::create_dir_all(&out_dir)?;
                osgb_dir_pair.push(OsgbInfo {
                    in_dir: osgb.to_string_lossy().into(),
                    out_dir: out_dir.to_string_lossy().into(),
                    sender: sender.clone(),
                });
            } else {
                error!("dir error: {}", osgb.display());
            }
        }
    }

    let rad_x = unsafe { degree2rad(center_x) };
    let rad_y = unsafe { degree2rad(center_y) };

    let max_lvl: i32 = max_lvl.unwrap_or(100);
    osgb_dir_pair
        .into_par_iter()
        .map(|info| unsafe {
            let mut root_box = vec![0f64; 6];
            let mut json_buf = vec![];
            let mut json_len = 0i32;
            let in_ptr = str_to_vec_c(&info.in_dir);
            let out_ptr = str_to_vec_c(&info.out_dir);
            let out_ptr = osgb23dtile_path(
                in_ptr.as_ptr(),
                out_ptr.as_ptr(),
                root_box.as_mut_ptr(),
                (&mut json_len) as *mut i32,
                rad_x,
                rad_y,
                max_lvl,
                enable_texture_compress,
                enable_meshopt,
                enable_draco_compress,
                enable_unlit,
            );
            if out_ptr.is_null() {
                error!("failed: {}", info.in_dir);
            } else {
                json_buf.resize(json_len as usize, 0);
                libc::memcpy(
                    json_buf.as_mut_ptr() as *mut libc::c_void,
                    out_ptr,
                    json_len as usize,
                );
                libc::free(out_ptr);
            }
            let t = TileResult {
                path: info.out_dir.into(),
                in_dir: info.in_dir,
                json: String::from_utf8(json_buf).unwrap(),
                box_v: root_box,
            };
            info.sender.send(t).unwrap();
        })
        .count();

    // merge and root
    let mut tile_array = vec![];
    for _ in 0..task_count {
        if let Ok(t) = receiver.recv() {
            if !t.json.is_empty() {
                tile_array.push(t);
            }
        }
    }
    let mut root_box = vec![-1.0E+38f64, -1.0E+38, -1.0E+38, 1.0E+38, 1.0E+38, 1.0E+38];
    let mut root_geometric_error = 0.0;
    for x in tile_array.iter() {
        for i in 0..3 {
            if x.box_v[i] > root_box[i] {
                root_box[i] = x.box_v[i]
            }
        }
        for i in 3..6 {
            if x.box_v[i] < root_box[i] {
                root_box[i] = x.box_v[i]
            }
        }
        let json_val: serde_json::Value = serde_json::from_str(&x.json).unwrap();
        if let Some(ge) = json_val["geometricError"].as_f64() {
            if ge > root_geometric_error {
                root_geometric_error = ge;
            }
        }
    }

    //let root_geometric_error = get_geometric_error(center_y, 10);
    // Use origin height: priority: origin_height > enu_offset.2 > region_offset calculation
    let tras_height = if let Some(h) = origin_height {
        h
    } else if let Some((_, _, enu_z)) = enu_offset {
        enu_z
    } else if let Some(v) = region_offset {
        v - root_box[5]
    } else {
        0f64
    };
    let mut trans_vec = vec![0f64; 16];
    unsafe {
        if let Some((enu_x, enu_y, enu_z)) = enu_offset {
            // Use the ENU-aware transform function
            transform_c_with_enu_offset(center_x, center_y, tras_height, enu_x, enu_y, enu_z, trans_vec.as_mut_ptr());
        } else {
            // Use standard transform function
            transform_c(center_x, center_y, tras_height, trans_vec.as_mut_ptr());
        }
    }
    let mut root_json = json!(
        {
            "asset": {
                "version": "1.0",
                "gltfUpAxis": "Z"
            },
            "geometricError": root_geometric_error * 2.0,
            "root" : {
                "transform" : trans_vec,
                "boundingVolume" : {
                    "box": box_to_tileset_box(&root_box)
                },
                "geometricError": root_geometric_error * 2.0,
                "refine": "REPLACE",
                "children": []
            }
        }
    );

    // Level B(顶层 LOD 金字塔):优先由 C++ 侧把全部 Tile 中位四叉分组,
    // 每组(含根)合并出减面+图集的 _pyramid b3dm,组成 REPLACE 精化树,
    // 叶占位 {"tile":i} 替换回对应 Tile 子树;任一环节失败回退平铺。
    let pyramid_node = if enable_pyramid && tile_array.len() >= 2 {
        try_build_pyramid(&tile_array, dir_dest, enable_draco_compress, enable_unlit)
    } else {
        None
    };
    if let Some(node) = pyramid_node {
        // 金字塔 ge 是逐层 2 倍累积的,层数多时会超过按 Tile ge 算出的 root ge。
        // 3D Tiles 要求 ge 自根向下单调不增,否则 Cesium 会直接跳过金字塔层
        // (父的误差比子还小,精化条件恒不满足)。这里把 root 抬到金字塔根之上。
        let pyr_ge = node["geometricError"].as_f64().unwrap_or(0.0);
        if pyr_ge * 2.0 > root_geometric_error * 2.0 {
            root_json["geometricError"] = json!(pyr_ge * 2.0);
            root_json["root"]["geometricError"] = json!(pyr_ge * 2.0);
        }
        root_json["root"]["children"]
            .as_array_mut()
            .unwrap()
            .push(node);
    } else {
        for x in tile_array {
            let path = x.path;
            let mut json_val: serde_json::Value = serde_json::from_str(&x.json).unwrap();
            // Level A(根节点内联合并):子 tileset 的 root 直接内联为根 children,
            // 不再生成 Data/Tile_*/tileset.json 外链,减少一层 HTTP 间接;
            // 子树内 b3dm uri 原是相对子目录的 "./X.b3dm",改写为相对根 tileset
            // 的 "./Data/<TileDir>/X.b3dm"。
            let tile_dir = Path::new(&path)
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .to_string();
            prefix_content_uris(&mut json_val, &format!("./Data/{}/", tile_dir));
            root_json["root"]["children"]
                .as_array_mut()
                .unwrap()
                .push(json_val);
        }
    }
    let path_json = dir_dest.join("tileset.json");
    let mut f = File::create(path_json)?;
    f.write_all(serde_json::to_string_pretty(&root_json).unwrap().as_bytes())?;
    Ok(())
}

#[allow(dead_code)]
fn get_geometric_error(center_y: f64, lvl: i32) -> f64 {
    use std::f64;
    let x = center_y * f64::consts::PI / 180.0;
    let round = x.cos() * 2.0 * f64::consts::PI * 6378137.0;
    let pow = 2i32.pow(lvl as u32 - 2);
    4.0 * round / (256 * pow) as f64
}

/// 递归改写节点树所有 content.uri:加目录前缀。
/// Level A 内联后,子树 b3dm uri 从相对子目录("./X.b3dm")
/// 变为相对根 tileset("./Data/<TileDir>/X.b3dm")。
fn prefix_content_uris(node: &mut serde_json::Value, prefix: &str) {
    if let Some(content) = node.get_mut("content") {
        if let Some(uri_val) = content.get_mut("uri") {
            if let Some(uri) = uri_val.as_str() {
                let new_uri = format!("{}{}", prefix, uri.strip_prefix("./").unwrap_or(uri));
                *uri_val = serde_json::Value::String(new_uri);
            }
        }
    }
    if let Some(children) = node.get_mut("children").and_then(|c| c.as_array_mut()) {
        for child in children.iter_mut() {
            prefix_content_uris(child, prefix);
        }
    }
}

/// Level B:调用 C++ build_top_pyramid 生成顶层 LOD 金字塔。
/// 成功返回金字塔根节点(叶占位已替换为各 Tile 子树),失败返回 None 走平铺回退。
fn try_build_pyramid(
    tile_array: &[TileResult],
    dir_dest: &Path,
    enable_draco: bool,
    enable_unlit: bool,
) -> Option<serde_json::Value> {
    use std::collections::HashMap;

    let pyramid_dir = dir_dest.join("Data").join("_pyramid");
    let mut tiles_spec = vec![];
    for (i, t) in tile_array.iter().enumerate() {
        let tile_dir = Path::new(&t.path).file_name()?.to_str()?.to_string();
        let (gx, gy) = match parse_tile_grid(&tile_dir) {
            Some(v) => v,
            None => {
                warn!("pyramid: cannot parse grid from {}, fallback", tile_dir);
                return None;
            }
        };
        let json_val: serde_json::Value = serde_json::from_str(&t.json).ok()?;
        let ge = json_val["geometricError"].as_f64().unwrap_or(0.0);
        tiles_spec.push(json!({
            "i": i,
            "osgb": t.in_dir,
            "gx": gx,
            "gy": gy,
            "ge": ge,
            "box": t.box_v,
        }));
    }
    let spec = json!({
        "out_pyramid_dir": pyramid_dir.to_string_lossy(),
        "atlas_px": 2048,
        "enable_draco": enable_draco,
        "enable_unlit": enable_unlit,
        "tiles": tiles_spec,
    });
    let spec_str = spec.to_string();
    let spec_c = str_to_vec_c(&spec_str);
    let mut out_len = 0i32;
    let out_ptr = unsafe { build_top_pyramid(spec_c.as_ptr(), spec_str.len() as i32, &mut out_len) };
    if out_ptr.is_null() {
        error!("pyramid: build_top_pyramid returned null");
        return None;
    }
    let mut buf = vec![0u8; out_len as usize];
    unsafe {
        libc::memcpy(
            buf.as_mut_ptr() as *mut libc::c_void,
            out_ptr,
            out_len as usize,
        );
        libc::free(out_ptr);
    }
    let resp: serde_json::Value = match serde_json::from_slice(&buf) {
        Ok(v) => v,
        Err(e) => {
            error!("pyramid: bad response json: {}", e);
            return None;
        }
    };
    if let Some(warns) = resp["warn"].as_array() {
        for w in warns.iter().filter_map(|w| w.as_str()) {
            warn!("pyramid: {}", w);
        }
    }
    if resp["ok"].as_bool() != Some(true) {
        error!(
            "pyramid: failed: {}",
            resp["error"].as_str().unwrap_or("unknown")
        );
        return None;
    }
    // 叶占位 {"tile":i} 替换为对应 Tile 子树(uri 前缀规则同 Level A 平铺)
    let mut tile_map: HashMap<usize, serde_json::Value> = HashMap::new();
    for (i, t) in tile_array.iter().enumerate() {
        let tile_dir = Path::new(&t.path).file_name()?.to_str()?.to_string();
        let mut json_val: serde_json::Value = serde_json::from_str(&t.json).ok()?;
        prefix_content_uris(&mut json_val, &format!("./Data/{}/", tile_dir));
        tile_map.insert(i, json_val);
    }
    let mut root_node = resp["root"].clone();
    if !substitute_tile_placeholders(&mut root_node, &tile_map) {
        error!("pyramid: unresolved tile placeholder, fallback");
        return None;
    }
    Some(root_node)
}

/// 从 Tile 目录名解析网格坐标,如 "Tile_+065_+052" -> (65.0, 52.0)。
fn parse_tile_grid(name: &str) -> Option<(f64, f64)> {
    let mut parts = name.split('_');
    if parts.next()? != "Tile" {
        return None;
    }
    let gx = parts.next()?.parse::<f64>().ok()?;
    let gy = parts.next()?.parse::<f64>().ok()?;
    Some((gx, gy))
}

/// 递归把金字塔叶占位 {"tile":i} 替换为 tile_map 中对应 Tile 子树。
fn substitute_tile_placeholders(
    node: &mut serde_json::Value,
    tile_map: &std::collections::HashMap<usize, serde_json::Value>,
) -> bool {
    if let Some(children) = node.get_mut("children").and_then(|c| c.as_array_mut()) {
        for child in children.iter_mut() {
            if let Some(ti) = child.get("tile").and_then(|v| v.as_u64()) {
                match tile_map.get(&(ti as usize)) {
                    Some(sub) => *child = sub.clone(),
                    None => return false,
                }
            } else if !substitute_tile_placeholders(child, tile_map) {
                return false;
            }
        }
    }
    true
}

fn box_to_tileset_box(box_v: &Vec<f64>) -> Vec<f64> {
    let mut box_new = vec![];
    box_new.push((box_v[0] + box_v[3]) / 2.0);
    box_new.push((box_v[1] + box_v[4]) / 2.0);
    box_new.push((box_v[2] + box_v[5]) / 2.0);

    box_new.push((box_v[3] - box_v[0]).abs() / 2.0);
    box_new.push(0.0);
    box_new.push(0.0);

    box_new.push(0.0);
    box_new.push((box_v[4] - box_v[1]).abs() / 2.0);
    box_new.push(0.0);

    box_new.push(0.0);
    box_new.push(0.0);
    box_new.push((box_v[5] - box_v[2]).abs() / 2.0);

    box_new
}

