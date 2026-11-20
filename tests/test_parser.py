from omnifetch.core.parser import parse_title


def test_bluray_movie_with_dash_group():
    work_key, q = parse_title("The.Matrix.1999.1080p.BluRay.x264-GRP")
    assert work_key == "the matrix"
    assert q.year == 1999
    assert q.resolution == 1080
    assert q.source == "bluray"
    assert q.codec == "x264"
    assert q.group == "grp"


def test_tv_episode_with_brackets():
    work_key, q = parse_title("[Fansub] Some Show - S01E02 [1080p][x265].mkv")
    assert work_key == "some show"
    assert q.season == 1
    assert q.episode == 2
    assert q.resolution == 1080
    assert q.codec == "x265"
    assert q.group == "fansub"


def test_crc_bracket_is_not_a_group():
    work_key, q = parse_title("[SubsPlease] Sousou no Frieren - 28 (1080p) [3A9D29C6].mkv")
    assert work_key == "sousou no frieren 28"
    assert q.resolution == 1080
    assert q.group == "subsplease"


def test_cjk_season_episode():
    work_key, q = parse_title("某作品 第二季 第五集 [2160p]")
    assert work_key == "某作品"
    assert q.season == 2
    assert q.episode == 5
    assert q.resolution == 2160


def test_plain_title_unchanged():
    work_key, q = parse_title("Minimal Title")
    assert work_key == "minimal title"
    assert q.resolution is None
    assert q.season is None
