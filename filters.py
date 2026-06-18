import os

# 只处理视频文件（音频、图片、字幕、nfo 等一律忽略，连下载都不下）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv", ".wmv",
    ".m4v", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".vob", ".m2ts", ".mts",
}


def is_video(name):
    """按扩展名判断是否视频文件（大小写不敏感）。"""
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def plan_files(files, max_file_bytes):
    """根据 aria2.getFiles 的文件清单，决定哪些下载、哪些跳过。

    参数 files：aria2.getFiles 返回，每项含 index / path / length。
    返回 (selected_indices, selected_paths, skipped)：
      - selected_indices：选中下载的 1-based 索引字符串列表（喂给 select-file）
      - selected_paths：  选中文件的绝对路径列表
      - skipped：         [(文件名, 字节大小, 原因)]，目前仅“视频但超过大小上限”
    非视频文件直接忽略，不计入 skipped。
    """
    selected_indices, selected_paths, skipped = [], [], []
    for f in files:
        path = f["path"]
        name = os.path.basename(path)
        length = int(f["length"])
        if not is_video(name):
            continue
        if length > max_file_bytes:
            skipped.append((name, length, "超过大小上限"))
            continue
        selected_indices.append(str(f["index"]))
        selected_paths.append(path)
    return selected_indices, selected_paths, skipped
