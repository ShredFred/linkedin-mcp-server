"""Fork-owned documentation policy, including panels reintroduced by a merge."""

from pathlib import Path
import runpy

import pytest

without_promotions = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/fork_readme.py")
)["without_promotions"]


def test_sponsor_panel_removal_preserves_nested_help_and_attribution():
    help_panel = "<details><summary>Setup help</summary><details><summary>Proxy</summary>Technical setup</details></details>"
    sponsor = "<details open><summary><strong>LinkedIn MCP Sponsor</strong></summary><details>Ad banner</details>Promo code</details>"
    source = "Upstream attribution\n" + help_panel + "\n" + sponsor + "\nApache License"
    actual = without_promotions(source)
    assert actual == "Upstream attribution\n" + help_panel + "\n\nApache License"
    assert without_promotions(actual) == actual


def test_unclosed_sponsor_panel_is_not_silently_truncated():
    with pytest.raises(ValueError, match="Unclosed"):
        without_promotions("<details><summary>LinkedIn MCP Sponsor</summary>Ad")


def test_nested_sponsor_panels_preserve_following_technical_content():
    source = "<details><summary>LinkedIn MCP Sponsor</summary><details><summary>Sponsored proxy providers</summary>AD</details></details>TECHNICAL INSTALLATION"
    assert without_promotions(source) == "TECHNICAL INSTALLATION"


def test_moved_promotion_requires_explicit_review():
    with pytest.raises(ValueError, match="outside a known panel"):
        without_promotions(
            "Install instructions\nVisit https://cadenza.page for a discount"
        )


def test_nested_ad_does_not_supply_its_summary_to_a_technical_parent():
    source = "<details>Technical setup before<details><summary>LinkedIn MCP Sponsor</summary>AD</details>Technical setup after</details>Outside"
    assert (
        without_promotions(source)
        == "<details>Technical setup beforeTechnical setup after</details>Outside"
    )


def test_entity_encoded_summary_is_recognized():
    assert (
        without_promotions(
            "<details><summary>LinkedIn&nbsp;MCP Sponsor</summary>AD</details>Setup"
        )
        == "Setup"
    )


def test_malformed_known_title_is_rejected():
    with pytest.raises(ValueError, match="outside a known panel"):
        without_promotions(
            "<details><summary>LinkedIn MCP Sponsor</details>Technical setup"
        )


@pytest.mark.parametrize("relative", ["README.md", "docs/docker-hub.md"])
def test_published_readme_corpus_has_no_known_commercial_promotion(relative):
    text = (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")
    for marker in [
        "LinkedIn MCP Sponsor",
        "cadenza.page",
        "golink.onl/unipile",
        "FOUNDING20",
    ]:
        assert marker not in text, (
            f"{relative} reintroduced upstream advertising: {marker}"
        )
