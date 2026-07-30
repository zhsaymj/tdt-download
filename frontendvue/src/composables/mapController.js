// 共享的地图控制器单例引用(MapView 创建后写入,其它组件读取调用)
import { shallowRef } from 'vue'

export const mapController = shallowRef(null)
