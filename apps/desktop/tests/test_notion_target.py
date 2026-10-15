"""同步目标的本地读取：只认文件夹里的 `.sync-config.yaml`，不碰网络。

标题不在这里——那是引擎 `pages` 查询的活儿；这份文件只回答「哪个页面」。
"""

from notion_sync_desktop.services.notion_target import Target, read_target


def write_config(folder, body: str) -> None:
    (folder / ".sync-config.yaml").write_text(body, encoding="utf-8")


def test_reads_the_parent_page_id(tmp_path):
    write_config(
        tmp_path,
        "notion:\n  parent_page_id: 3f9fa72484e880f0a576d312267745ec\n"
        "  api_version: '2025-09-03'\n",
    )

    target = read_target(tmp_path)

    assert target == Target("3f9fa72484e880f0a576d312267745ec")
    assert target.url == "https://www.notion.so/3f9fa72484e880f0a576d312267745ec"


def test_dashes_are_stripped_from_the_url(tmp_path):
    write_config(
        tmp_path, "notion:\n  parent_page_id: 290f94bc-a2be-4c0a-8190-8188f2a145b9\n"
    )
    assert read_target(tmp_path).url == (
        "https://www.notion.so/290f94bca2be4c0a81908188f2a145b9"
    )


def test_missing_config_is_none(tmp_path):
    assert read_target(tmp_path) is None


def test_folder_that_does_not_exist_is_none(tmp_path):
    assert read_target(tmp_path / "not-there") is None


def test_config_without_a_parent_page_is_none(tmp_path):
    write_config(tmp_path, "include: ['*.md']\n")
    assert read_target(tmp_path) is None


def test_broken_config_is_none_not_an_exception(tmp_path):
    write_config(tmp_path, "notion: [unclosed\n")
    assert read_target(tmp_path) is None


def test_non_dict_notion_block_is_none(tmp_path):
    write_config(tmp_path, "notion: 42\n")
    assert read_target(tmp_path) is None
