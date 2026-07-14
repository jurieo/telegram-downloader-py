from common.filters import is_video, plan_files

GB = 1024 ** 3


def test_is_video():
    assert is_video("ep1.mp4")
    assert is_video("EP2.MKV")          # 大小写不敏感
    assert not is_video("sub.srt")
    assert not is_video("info.nfo")
    assert not is_video("cover.jpg")
    assert not is_video("readme.txt")


def test_plan_files_selects_only_videos_within_limit():
    files = [
        {"index": "1", "path": "/d/show/ep1.mkv", "length": str(2 * GB)},
        {"index": "2", "path": "/d/show/ep2.mkv", "length": str(6 * GB)},  # 超过 4GB
        {"index": "3", "path": "/d/show/sub.srt", "length": "1024"},        # 非视频
        {"index": "4", "path": "/d/show/cover.jpg", "length": "2048"},      # 非视频
    ]
    indices, paths, skipped = plan_files(files, 4 * GB)
    assert indices == ["1"]
    assert paths == ["/d/show/ep1.mkv"]
    assert len(skipped) == 1
    assert skipped[0][0] == "ep2.mkv"
    assert skipped[0][2] == "超过大小上限"


def test_plan_files_empty_when_no_video():
    files = [
        {"index": "1", "path": "/d/x/a.srt", "length": "10"},
        {"index": "2", "path": "/d/x/b.jpg", "length": "10"},
    ]
    indices, paths, skipped = plan_files(files, 4 * GB)
    assert indices == []
    assert paths == []
    assert skipped == []


def test_plan_files_boundary_equal_limit_is_kept():
    # 恰好等于上限应当保留（只有“超过”才跳过）
    files = [{"index": "1", "path": "/d/x/a.mp4", "length": str(4 * GB)}]
    indices, paths, skipped = plan_files(files, 4 * GB)
    assert indices == ["1"]
    assert skipped == []
