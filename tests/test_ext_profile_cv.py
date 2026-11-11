"""Browser-free checks of the CV tools: validation, diff/plan, dates, pacing.

Fantasy data only (Erika Beispiel, Musterwerk GmbH).
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin import ext_profile_cv as cvx
from linkedin_mcp_server.tools import ext_profile_cv as tc

CV_YAML = """
headline: Werkstoffprüferin bei Musterwerk
about: |
  Ich prüfe Gefüge.
location: {country: Deutschland, city: Musterstadt}
experience:
  - title: Werkstoffprüferin
    company: Musterwerk GmbH
    location: Musterstadt
    employment_type: Vollzeit
    current: true
    start: 2021-03
    description: Gefügeanalysen und Härteprüfung.
    skills: [Metallographie]
  - title: Laborantin
    company: Beispiel AG
    start: 2016-09
    end: 2021-02
education:
  - school: Hochschule Musterstadt
    degree: B.Sc.
    field: Werkstofftechnik
    start: 2012
    end: 2016
certifications:
  - name: Beispielzertifikat Härteprüfung
    issuer: Prüfverband Muster
    issued: 2022-05
    credential_id: ABC-123
    credential_url: https://example.org/zertifikat/abc-123
languages:
  - Deutsch
  - {name: Englisch, proficiency: Verhandlungssicher}
publications:
  - title: Gefüge im Blick
    publisher: Musterverlag
    date: 2023-04-17
"""


def _item(values, href="/in/erika-beispiel/edit/forms/position/1/"):
    return {"values": values, "href": href}


def test_cv_budget_is_paced_write() -> None:
    assert outreach.PACE_BUDGETS["profile_cv_edit"] == {"day": 30, "week": 60}
    assert "profile_cv_edit" in outreach.PACE_WRITE_KINDS
    assert outreach.PACE_BUDGETS["profile_edit"] == {"day": 6, "week": 20}


def test_load_and_check_valid_cv() -> None:
    cv = tc.load_cv(CV_YAML)
    assert tc.check_cv(cv) is None
    assert cv["languages"][0] == {"name": "Deutsch"}  # shorthand normalised


def test_json_is_accepted() -> None:
    assert tc.load_cv('{"languages": ["Deutsch"]}') == {"languages": ["Deutsch"]}


@pytest.mark.parametrize(
    "cv,field",
    [
        ({"hobbies": []}, "cv"),
        ({"experience": [{"title": "X"}]}, "experience[0]"),
        (
            {"experience": [{"title": "X", "company": "Y", "salary": 1}]},
            "experience[0]",
        ),
        (
            {"experience": [{"title": "X", "company": "Y", "start": "03/2021"}]},
            "experience[0].start",
        ),
        (
            {"experience": [{"title": "X", "company": "Y", "start": "2021-13"}]},
            "experience[0].start",
        ),
        (
            {"experience": [{"title": "X", "company": "Y", "current": "ja"}]},
            "experience[0].current",
        ),
        (
            {
                "certifications": [
                    {"name": "N", "issuer": "I", "credential_url": "http://x.org"}
                ]
            },
            "certifications[0].credential_url",
        ),
        ({"projects": [{"title": "a\nb"}]}, "projects[0].title"),
        ({"languages": ["Deutsch", "deutsch"]}, "languages[1]"),
        ({"location": {"street": "x"}}, "location"),
    ],
)
def test_invalid_cv_refused(cv, field) -> None:
    got = tc.check_cv(cv)
    assert got is not None and got["status"] == "invalid_input"
    assert got["field"] == field


def test_parse_date_and_date_text() -> None:
    assert cvx.parse_date("2021-03") == ("2021", "3", "")
    assert cvx.parse_date("2016") == ("2016", "", "")
    assert cvx.date_text("2023-04-17", "tt.mm.jjjj") == "17.04.2023"
    assert cvx.date_text("2023-04-17", "mm/dd/yyyy") == "04/17/2023"
    with pytest.raises(ValueError):
        cvx.date_text("2023-04", "tt.mm.jjjj")


def test_plan_create_update_and_skip_delete() -> None:
    target = tc.load_cv(CV_YAML)
    current = {
        "headline": {"headline": "Werkstoffprüferin bei Musterwerk"},
        "experience": [
            _item(
                {
                    "title": "Werkstoffprüferin",
                    "company": "Musterwerk GmbH",
                    "start": "2021-03",
                    "current": True,
                    "location": "Musterstadt",
                    "employment_type": "Vollzeit",
                    "description": "Alt.",
                }
            ),
            _item(
                {"title": "Praktikantin", "company": "Altfirma"},
                "/in/x/edit/forms/position/2/",
            ),
        ],
    }
    plan = tc.plan_cv(
        {"headline": target["headline"], "experience": target["experience"]},
        current,
        allow_delete=False,
    )
    ops = [(s["op"], s["section"]) for s in plan["steps"]]
    assert ops == [("update", "experience"), ("create", "experience")]
    assert plan["steps"][0]["fields"] == ["description"]
    assert plan["steps"][0]["href"].endswith("/position/1/")
    reasons = {s["reason"] for s in plan["skipped"]}
    assert {"delete_not_allowed", "skills_not_measured"} <= reasons


def test_plan_delete_only_when_allowed_and_after_creates() -> None:
    current = {"languages": [_item({"name": "Latein"}, "/in/x/edit/forms/language/9/")]}
    plan = tc.plan_cv({"languages": [{"name": "Deutsch"}]}, current, allow_delete=True)
    assert [s["op"] for s in plan["steps"]] == ["create", "delete"]
    assert plan["steps"][1]["pre_image"] == {"name": "Latein"}


def test_plan_idempotent_when_equal() -> None:
    current = {
        "languages": [
            _item(
                {"name": "Deutsch", "proficiency": "Muttersprache"},
                "/in/x/edit/forms/language/1/",
            )
        ]
    }
    plan = tc.plan_cv({"languages": [{"name": "deutsch"}]}, current, allow_delete=False)
    assert plan["steps"] == []


def test_plan_never_writes_unstructured_section() -> None:
    current = {"education": [{"values": None, "href": None, "lines": ["Hochschule"]}]}
    plan = tc.plan_cv(
        {"education": [{"school": "H", "degree": "D"}]}, current, allow_delete=True
    )
    assert plan["steps"] == []
    assert plan["skipped"][0]["reason"] == "current_unstructured"


def test_plan_refuses_duplicate_current_keys() -> None:
    a = _item({"name": "Deutsch"}, "/in/x/edit/forms/language/1/")
    b = _item({"name": "Deutsch"}, "/in/x/edit/forms/language/2/")
    plan = tc.plan_cv(
        {"languages": [{"name": "Deutsch"}]}, {"languages": [a, b]}, allow_delete=True
    )
    assert (
        plan["steps"] == [] and plan["skipped"][0]["reason"] == "current_duplicate_key"
    )


def test_unsupported_sections_reported() -> None:
    plan = tc.plan_cv({"skills": ["Metallographie"]}, {}, allow_delete=False)
    assert plan["skipped"][0]["reason"] == "unsupported"


def test_singletons_and_headline_planned() -> None:
    plan = tc.plan_cv(
        {"about": "Neu", "headline": "H", "location": {"city": "Musterstadt"}},
        {"about": {"about": "Alt"}, "headline": {"headline": "H"}, "location": {}},
        allow_delete=False,
    )
    assert [(s["op"], s["section"]) for s in plan["steps"]] == [
        ("update", "about"),
        ("update", "location"),
    ]


def test_step_key_stable_and_distinct() -> None:
    s1 = {
        "op": "create",
        "section": "languages",
        "key": ["deutsch"],
        "values": {"name": "Deutsch"},
    }
    s2 = {**s1, "values": {"name": "Deutsch", "proficiency": "Muttersprache"}}
    assert tc.step_key("x", s1) == tc.step_key("x", dict(s1))
    assert tc.step_key("x", s1) != tc.step_key("x", s2)
    assert tc.step_key("x", s1) != tc.step_key("y", s1)


def test_compare_and_form_values() -> None:
    spec = cvx.SECTIONS["experience"]["fields"]
    form = {
        "fields": {
            "title": {"value": "Werkstoffprüferin"},
            "start": {"year": "2021", "month": "3"},
            "current": {"value": True},
            "employment_type": {"value": "Vollzeit"},
        }
    }
    assert (
        cvx.compare(
            spec,
            {
                "title": "werkstoffprüferin",
                "start": "2021-03",
                "current": True,
                "employment_type": "Vollzeit",
            },
            form,
        )
        == {}
    )
    diff = cvx.compare(spec, {"start": "2021-04"}, form)
    assert diff == {"start": (("2021", "3"), ("2021", "4"))}
    values = cvx.form_values(spec, form)
    assert values["start"] == "2021-03" and values["current"] is True


def test_forbidden_checkbox_listed() -> None:
    # The end-current-position checkbox must stay a guard, never a field.
    assert any("sofortiger wirkung" in w for w in cvx.FORBIDDEN_CHECK)
    labels = [
        lab
        for spec in cvx.SECTIONS.values()
        for _, ls in spec["fields"].values()
        for lab in ls
    ]
    assert not any("sofortiger" in lab for lab in labels)


def test_to_target_round_trip_plans_nothing() -> None:
    read = {
        "headline": {"headline": "H"},
        "about": {"about": "A"},
        "languages": [_item({"name": "Deutsch"}, "/in/x/edit/forms/language/1/")],
    }
    target = tc.to_target(read)
    assert tc.check_cv(target) is None
    assert tc.plan_cv(target, read, allow_delete=True)["steps"] == []
