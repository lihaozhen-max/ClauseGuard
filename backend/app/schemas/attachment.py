"""附件相关的出入参模型（SPEC IF-03）。"""

from __future__ import annotations

from pydantic import BaseModel


class AttachmentDownloadResult(BaseModel):
    """IF-03 返回（SPEC §5.1）。

    ``file_path`` 为**项目根目录相对路径**（存库口径，不随进程 CWD 漂移），
    非 SPEC 示例中的 ``./storage/...`` 形式——见 ``docs/M2-验收记录.md`` 的偏离说明。
    """

    task_id: int
    attachment_id: str
    file_name: str
    file_type: str
    file_path: str
    file_size: int
    file_checksum: str
    download_status: str
