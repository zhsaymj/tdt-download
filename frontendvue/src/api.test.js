import test from 'node:test'
import assert from 'node:assert/strict'

import { formatApiDetail } from './api.js'

test('FastAPI 校验错误对象显示为可读文本', () => {
  const detail = [
    { loc: ['body', 'containers', 'geotiff'], msg: 'Input should be a valid string' },
  ]
  assert.equal(
    formatApiDetail(detail),
    'body.containers.geotiff: Input should be a valid string',
  )
})

test('普通对象错误不会显示为 [object Object]', () => {
  assert.equal(formatApiDetail({ error: 'bad container' }), '{"error":"bad container"}')
})
