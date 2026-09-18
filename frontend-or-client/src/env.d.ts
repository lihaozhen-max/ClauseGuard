/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 可选的构建期默认值；留空时由页面上的"接口密钥"对话框写入 localStorage。 */
  readonly VITE_INTERNAL_API_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
