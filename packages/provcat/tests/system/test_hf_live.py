import hashlib
import os

import pytest

from provcat import Catalog, NotFound

pytestmark = [pytest.mark.system,
              pytest.mark.skipif(not os.environ.get("PROVCAT_SYSTEM_HF"), reason="set PROVCAT_SYSTEM_HF=1 to call huggingface.co")]

GPT2 = "hf://models/hf-internal-testing/tiny-random-gpt2"
GPT2_SHA = "71034c5d8bde858ff824298bdedc65515b97d2b9"  # main since 2024


def cat(tmp_path, offline=False):
    return Catalog({"stores": {"hf": {"token_env": []}}}, cache_root=tmp_path / "cache", offline=offline)


def test_live_hf_file_kept_in_xet(tmp_path):
    c = cat(tmp_path)
    assert c.pin(f"{GPT2}?at=commit:71034c5d").selector.value == GPT2_SHA
    p = c.fetch(f"{GPT2}/model.safetensors?at=commit:{GPT2_SHA}")
    assert p.stat().st_size == 453864
    assert hashlib.sha256(p.read_bytes()).hexdigest().startswith("8111d5afb071")
    assert c.stat(f"{GPT2}/config.json?at=commit:{GPT2_SHA}").size == 807


def test_live_hf_folder_and_offline(tmp_path):
    c = cat(tmp_path)
    d = c.fetch(f"{GPT2}?at=commit:{GPT2_SHA}")
    assert (d / "config.json").stat().st_size == 807 and (d / "tf_model.h5").stat().st_size == 8462256
    off = cat(tmp_path, offline=True)
    assert off.fetch(f"{GPT2}/vocab.json?at=commit:{GPT2_SHA}").stat().st_size == 10419


def test_live_hf_branches_and_ts(tmp_path):
    c = cat(tmp_path)
    ds = "hf://datasets/hf-internal-testing/fixtures_image_utils"
    assert c.pin(f"{ds}?at=branch:master").selector.value.startswith("8e7ac0fa")
    assert c.pin(f"{GPT2}?at=ts:2023-01-01T00:00:00Z").selector.value.startswith("937b4d23")
    with pytest.raises(NotFound):
        c.pin(f"{GPT2}?at=ts:2015-01-01")
    with pytest.raises(NotFound):
        c.stat(f"{GPT2}/no-such-file?at=commit:{GPT2_SHA}")


def test_live_hf_ts_reads_later_pages(tmp_path):
    """open-llm-leaderboard/contents has hundreds of commits; this one is past the first page."""
    c = cat(tmp_path)
    ds = "hf://datasets/open-llm-leaderboard/contents"
    assert c.pin(f"{ds}?at=ts:2025-03-04T15:41:03Z").selector.value == "fac62252ff0bcd1bf7d019eb844f91b3d6582475"


def test_live_hf_dataset_file(tmp_path):
    c = cat(tmp_path)
    p = c.fetch("hf://datasets/hf-internal-testing/fixtures_image_utils/README.md?at=branch:main")
    assert p.stat().st_size == 365


def test_live_hf_writes_nothing_to_the_hub_cache(tmp_path):
    """huggingface_hub reads HF_HOME when it is imported, so this runs in a new process."""
    import subprocess
    import sys
    home = tmp_path / "hf_home"
    code = ("from provcat import Catalog; import sys; "
            "c = Catalog({'stores': {'hf': {'token_env': []}}}, cache_root=sys.argv[1]); "
            f"c.fetch('{GPT2}/model.safetensors?at=commit:{GPT2_SHA}'); "
            f"c.fetch('{GPT2}/config.json?at=commit:{GPT2_SHA}')")
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "cache")], check=True,
                   env={**os.environ, "HF_HOME": str(home)})
    big = [p for p in home.rglob("*") if p.is_file() and p.stat().st_size > 100_000]
    assert not (home / "hub").exists() and big == []
