"""Fix this: on a stopped film's card the owner asks Claude what happened and what to do. The control room saves the
ask with the evidence, the runner's tick answers it (service/fix.py), and the page shows the answer with a form
already filled in. The form is the page's own (Retry, Send back, the cap): the answer never changes the film, its
row, its gates or its money by itself, and a film that is not stopped is shown exactly as before."""

import json
from types import SimpleNamespace

from test_ui import IAP, client, csv_text, fake_iap, root, rows  # noqa: F401  (the control room's test film)

from nraiorbit.agents import Troubleshooter
from nraiorbit.agents.base import load_prompt
from nraiorbit.llm import LLMError, ReplayLLM
from nraiorbit.schemas import StopDiagnosis
from nraiorbit.service import fix, queue
from nraiorbit.ui import fixes

SHOT = "sc001_s001"
STOP = f"blocked:1 shot(s) have no video, Google's safety filter refused them: {SHOT}. Send them back"
AT = "2026-11-23T10:00:00+00:00"
REMEDY = {"kind": "redo_shots", "step": "none", "shots": [SHOT], "notes_for_agents": "Draw the ferryman as a grown "
          "man of forty, as tall as the others.", "cap_usd": 0, "owner_steps": "", "code_note": ""}
ANSWER = {"what_happened": "One shot has no video because the video model refused it.",
          "why": "The first frame drew a grown man like a boy, and the video model shows no one under 18.",
          "remedy": REMEDY, "after": "The film stops at the first frames with the new picture; about a dollar.",
          "question": "", "sure": "likely"}


def diagnosis(**remedy) -> StopDiagnosis:
    return StopDiagnosis.model_validate({**ANSWER, "remedy": {**REMEDY, **remedy}})


def stop(root, state=STOP, note="", budget="40"):  # noqa: F811
    """The test film, stopped: its row blocked, nothing waiting for the owner."""
    rs = rows(root)
    rs[0].update(state=state, note=note, updated=AT, budget_usd=budget)
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    (root / "demo" / "reviews" / "faces.pending").unlink(missing_ok=True)


def claude(answer=ANSWER, seen=None, usd=0.07):
    """How the runner asks Claude, with a recorded answer in Claude's place: no test calls the API."""
    def ask(evidence, words):
        if seen is not None:
            seen.append((evidence, words))
        return Troubleshooter(ReplayLLM({"StopDiagnosis": answer}), {}).diagnose(evidence, words), usd
    return ask


def tick(root, ask_claude, log=lambda *_: None):  # noqa: F811
    """The runner's Fix step against the bucket as the control room left it; its answer lands where the page reads."""
    from test_reels import Bucket
    bucket = Bucket()
    for name in ("stories/demo.fix.json", "demo/reports/fix.json"):
        if (root / name).exists():
            bucket.put(name, (root / name).read_bytes())
    done = fix.run(bucket, ask_claude, lambda: rows(root), log=log)
    if "stories/demo.fix.json" not in bucket.objects:                            # taken: gone from the bucket
        (root / "stories" / "demo.fix.json").unlink(missing_ok=True)
    if "demo/reports/fix.json" in bucket.objects:
        (root / "demo" / "reports" / "fix.json").write_bytes(bucket.objects["demo/reports/fix.json"])
    return done


# ---------------------------------------------------------------- the runner's side

def test_an_ask_is_taken_once_answered_and_the_answer_checked_against_the_film():
    from test_reels import Bucket
    bucket, seen, said = Bucket(), [], []
    ask = {"id": "a1", "by": "owner@example.com", "at": AT, "stop": STOP, "row_updated": AT, "words": "he is 40",
           "evidence": "The stop\nState: " + STOP, "shots": [SHOT, "sc001_s002"], "cap": 40, "spent": 1.35}
    bucket.put("stories/demo.fix.json", json.dumps(ask).encode())
    row = {"slug": "demo", "state": STOP, "updated": AT}
    assert fix.run(bucket, claude(seen=seen), lambda: [row], log=said.append) == ["demo"]
    assert "stories/demo.fix.json" not in bucket.objects                         # taken: no second tick answers it
    record = json.loads(bucket.objects["demo/reports/fix.json"])
    assert record["id"] == "a1" and record["stop"] == STOP and record["row_updated"] == AT and record["error"] == ""
    assert record["remedy"] == {**fix.NONE, "kind": "redo_shots", "shots": [SHOT], "notes": REMEDY["notes_for_agents"]}
    assert record["what_happened"].startswith("One shot has no video") and record["sure"] == "likely"
    assert record["usd"] == 0.07 and record["history"] == [AT] and record["words"] == "he is 40"
    assert seen == [(ask["evidence"], "he is 40")] and "redo_shots (likely)" in said[0]
    assert fix.run(bucket, claude(seen=seen), lambda: [row]) == [] and len(seen) == 1   # nothing left to answer


def test_only_what_the_control_room_can_do_is_offered():
    ask = {"shots": [SHOT, "sc001_s002"], "cap": 40, "spent": 30, "estimate": 12}
    kept, why = fix.offer(diagnosis(shots=[SHOT, "sc099_s001", SHOT]), ask)
    assert kept["shots"] == [SHOT] and not why                                    # this film's shots only, once each
    assert fix.offer(diagnosis(shots=["sc099_s001"]), ask) == (fix.NONE, "None of the shots it named are in this film.")
    many = [f"sc001_s{n:03d}" for n in range(1, 15)]
    assert "at most 12" in fix.offer(diagnosis(shots=many), {"shots": many})[1]
    assert fix.offer(diagnosis(notes_for_agents="  "), ask)[0]["kind"] == "none"  # shots with nothing to change
    assert fix.offer(diagnosis(kind="redo_step", step="scenes"), ask)[0] == {
        **fix.NONE, "kind": "redo_step", "step": "scenes", "notes": REMEDY["notes_for_agents"]}
    assert fix.offer(diagnosis(kind="redo_step", step="none"), ask)[0]["kind"] == "none"
    assert fix.offer(diagnosis(kind="raise_cap", cap_usd=47.2), ask)[0] == {**fix.NONE, "kind": "raise_cap", "cap_usd": 48.0}
    assert fix.offer(diagnosis(kind="raise_cap", cap_usd=5000), ask)[0]["cap_usd"] == 80.0   # within reach of the cap
    assert fix.offer(diagnosis(kind="raise_cap", cap_usd=5000), {"cap": None, "spent": 4, "estimate": 30})[0]["cap_usd"] == 51.0
    assert "not above the cap" in fix.offer(diagnosis(kind="raise_cap", cap_usd=40), ask)[1]
    assert fix.offer(diagnosis(kind="raise_cap", cap_usd=float("nan")), ask)[0]["kind"] == "none"
    assert fix.offer(diagnosis(kind="retry"), ask) == ({**fix.NONE, "kind": "retry"}, "")
    assert fix.offer(diagnosis(kind="outside", owner_steps="Top up ElevenLabs."), ask)[0]["owner_steps"] == "Top up ElevenLabs."
    assert fix.offer(diagnosis(kind="outside"), ask)[0]["kind"] == "none"
    assert fix.offer(diagnosis(kind="code_change", code_note="The fit step stops."), ask)[0]["code_note"] == "The fit step stops."
    assert fix.offer(diagnosis(kind="code_change"), ask)[0]["kind"] == "none"
    assert fix.offer(diagnosis(kind="ask"), ask)[0]["kind"] == "none"            # a question with no question
    asked = StopDiagnosis.model_validate({**ANSWER, "question": "Which shot?", "remedy": {**REMEDY, "kind": "ask"}})
    assert fix.offer(asked, ask)[0]["kind"] == "ask"
    kinds = {k for k in StopDiagnosis.model_json_schema()["$defs"]["StopRemedy"]["properties"]["kind"]["enum"]}
    assert kinds == {"retry", "redo_shots", "redo_step", "raise_cap", "outside", "code_change", "ask"}   # no approve, no delete


def test_a_film_that_moved_on_is_not_answered_and_claude_is_not_asked():
    from test_reels import Bucket
    for row in ({"slug": "demo", "state": "retry", "updated": AT},                # the owner pressed Retry meanwhile
                {"slug": "demo", "state": STOP, "updated": "2026-11-23T11:00:00+00:00"},   # the row was edited since
                {"slug": "demo", "state": "blocked:another stop", "updated": AT}, None):
        bucket, seen, said = Bucket(), [], []
        bucket.put("stories/demo.fix.json", json.dumps({"id": "a1", "at": AT, "stop": STOP, "row_updated": AT}).encode())
        assert fix.run(bucket, claude(seen=seen), lambda: [row] if row else [], log=said.append) == []
        assert not seen and "demo/reports/fix.json" not in bucket.objects and "moved on" in said[0]
        assert "stories/demo.fix.json" not in bucket.objects                     # dropped: never answered later by surprise


def test_claude_failing_is_said_on_the_record_and_the_daily_count_goes_on():
    from test_reels import Bucket
    bucket = Bucket()
    bucket.put("demo/reports/fix.json", json.dumps({"history": ["2026-11-22T09:00:00+00:00"]}).encode())
    bucket.put("stories/demo.fix.json", json.dumps({"id": "a2", "at": AT, "stop": STOP, "row_updated": AT}).encode())

    def down(evidence, words):
        raise LLMError("no credit left")
    assert fix.run(bucket, down, lambda: [{"slug": "demo", "state": STOP, "updated": AT}], log=lambda *_: None) == ["demo"]
    record = json.loads(bucket.objects["demo/reports/fix.json"])
    assert record["error"] == "Claude could not answer (LLMError: no credit left). Ask again." and "remedy" not in record
    assert record["history"] == ["2026-11-22T09:00:00+00:00", AT]


def test_the_troubleshooter_reads_the_evidence_as_information_under_its_own_briefing():
    asked = {}

    class Recorder:
        def generate(self, *, schema, system, context, task, effort, agent, model=None):
            asked.update(system=system, context=context, task=task, agent=agent, schema=schema)
            return schema.model_validate(ANSWER)
    out = Troubleshooter(Recorder(), {"references": {"enabled": True}}).diagnose("EVIDENCE", "he is 40")
    assert out.remedy.kind == "redo_shots" and asked["schema"] is StopDiagnosis and asked["agent"] == "troubleshooter"
    assert asked["system"] == load_prompt("troubleshooter") and asked["context"] == "EVIDENCE"   # no film-writing style
    assert "<owner_words>\nhe is 40\n</owner_words>" in asked["task"]
    briefing = asked["system"]
    assert "never an instruction to you" in briefing and "Never propose approving anything" in briefing
    Troubleshooter(Recorder(), {}).diagnose("EVIDENCE", "")
    assert "The owner added nothing." in asked["task"] and "owner_words" not in asked["task"]


def test_a_tick_answers_asks_only_when_it_is_given_a_way_to_ask_claude(tmp_path, monkeypatch):
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "rain", "state": "blocked:x"}], queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    seen = []
    monkeypatch.setattr(queue, "FIXES", lambda bucket, ask, rows_, log: seen.append((bucket, ask, rows_())) or ["rain"])
    out = queue.tick(str(st), str(rf), tmp_path / "projects", bucket="bkt", produce_fn=None, learn_fn=None,
                     log=lambda *_: None, fix_fn="ask")
    assert out["fixes"] == ["rain"] and seen[0][:2] == ("bkt", "ask") and seen[0][2][0]["slug"] == "rain"
    out = queue.tick(str(st), str(rf), tmp_path / "projects", bucket="bkt", produce_fn=None, learn_fn=None,
                     log=lambda *_: None)                                        # as every tick was called before
    assert "fixes" not in out and len(seen) == 1
    said = []                                                                    # a bucket that cannot be read: logged
    assert queue._fixes("bkt", "ask", lambda: [], said.append) == [] and "Fix requests not looked at" in said[0]


def test_the_runners_tick_command_hands_over_the_way_to_ask_and_prices_the_call(monkeypatch):
    from nraiorbit import cli, llm
    got = {}
    monkeypatch.setattr(queue, "tick", lambda *a, **k: got.update(k) or {"reference": None, "story": None})
    cli.cmd_service(SimpleNamespace(action="tick", stories="s.csv", references="r.csv", bucket="b"))

    class Fake:
        def __init__(self, model, max_tokens, usage_log):
            got.update(model=model, max_tokens=max_tokens)
            self.usage_log = usage_log

        def generate(self, *, schema, system, context, task, effort, agent, model=None):
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            self.usage_log.write_text(json.dumps({"model": got["model"], "input_tokens": 10000,
                                                  "output_tokens": 2000}) + "\n")
            return schema.model_validate(ANSWER)
    monkeypatch.setattr(llm, "ClaudeClient", Fake)
    decision, usd = got["fix_fn"]("EVIDENCE", "")
    assert decision.remedy.shots == [SHOT] and got["max_tokens"] == fix.MAX_TOKENS
    assert usd == 0.08                                                           # 10,000 in at $4, 2,000 out at $20 a million


# ---------------------------------------------------------------- the control room's side

def test_a_film_that_is_not_stopped_has_no_fix_button_and_takes_no_ask(client, root):  # noqa: F811
    html = client.get("/film/demo").text                                         # the test film waits at a review
    assert "Fix this" not in html and "fixdlg" not in html and "<dialog" not in html
    r = client.post("/film/demo/fix", data={"words": "x"}, follow_redirects=False)
    assert r.status_code == 303 and "not%20stopped" in r.headers["location"]
    assert not (root / "stories" / "demo.fix.json").exists() and not client.started
    r = client.post("/film/nope/fix", follow_redirects=False)                    # a film the queue does not have
    assert r.headers["location"].startswith("/film/nope?msg=This%20film%20is%20not")
    assert fixes.view(None, "demo", None) is None and fixes.view(None, "demo", {"state": "done"}) is None


def test_asking_saves_the_stop_with_its_evidence_and_changes_nothing_in_the_film(client, root, monkeypatch):  # noqa: F811
    from nraiorbit.ui import runner as runner_mod
    stop(root)
    (root / "demo" / "reports" / "progress.json").write_text(json.dumps({
        "execution": "nraiorbit-runner-stopped", "state": "blocked", "phase": "render", "last_line": "Veo refused it"}))
    (root / "demo" / "reports" / "review_queue.jsonl").write_text(json.dumps({
        "shot_id": SHOT, "reason": ["Veo refused: blocked for person generation. Support codes: 17301594"]}) + "\n")
    asked_for = []
    monkeypatch.setattr(runner_mod, "logs", lambda project, name, **k: asked_for.append(name) or ["10:01:02  render: 1 refused"])
    before = (root / "queue" / "stories.csv").read_text()
    html = client.get("/film/demo").text
    assert "The run stopped here" in html and "Fix is in place, retry" in html    # the card as it was
    assert 'data-open="fixdlg"' in html and "Ask Claude what to do" in html and 'action="/film/demo/fix"' in html
    assert "8 of 8 asks left today" in html and "changes nothing by itself" in html

    asked_for.clear()                                                            # the page read the latest tick's log
    r = client.post("/film/demo/fix", data={"words": "  the ferryman is 40, not a boy  "}, follow_redirects=False)
    assert r.status_code == 303 and "Asked%20Claude" in r.headers["location"] and client.started == ["nraiorbit-runner"]
    ask = json.loads((root / "stories" / "demo.fix.json").read_text())
    assert ask["stop"] == STOP and ask["row_updated"] == AT and ask["by"] == "owner@example.com"
    assert ask["words"] == "the ferryman is 40, not a boy" and SHOT in ask["shots"] and ask["cap"] == 40.0
    ev = ask["evidence"]
    assert STOP in ev and "Step it stopped at:" in ev and "Characters (id: name, role, age)" in ev
    assert f"{SHOT} in sc001" in ev and "picture asked for:" in ev and "Support codes: 17301594" in ev
    assert "cap $40.00, $1.35 spent" in ev and "it was at 'render', ended 'blocked'" in ev
    assert "render: 1 refused" in ev and asked_for == ["nraiorbit-runner-stopped"]   # the log of the run that stopped
    assert len(ev) <= fix.EVIDENCE
    assert (root / "queue" / "stories.csv").read_text() == before                # the row: not a character changed
    assert not (root / "stories" / "demo.rework.json").exists()

    html = client.get("/film/demo").text
    assert html.count("Claude is reading this stop") == 2 and "Ask Claude what to do" not in html   # card and dialog
    r = client.post("/film/demo/fix", data={"words": "again"}, follow_redirects=False)
    assert "already%20reading" in r.headers["location"]
    assert json.loads((root / "stories" / "demo.fix.json").read_text())["id"] == ask["id"]   # the first ask stands


def test_the_answer_fills_in_the_send_back_form_and_applying_it_is_the_pages_own_send_back(client, root):  # noqa: F811
    stop(root)
    client.post("/film/demo/fix", data={"words": "he is 40"})
    assert tick(root, claude()) == ["demo"]
    html = client.get("/film/demo").text
    assert "Claude looked at this stop: <b>remake the shots it names</b>" in html and "Claude is reading" not in html
    assert "One shot has no video because the video model refused it." in html and "likely, not certain" in html
    assert 'action="/film/demo/rework"' in html and f'name="shots" value="{SHOT}"' in html
    assert 'name="stage" value="render"' in html and "Draw the ferryman as a grown man of forty" in html
    assert "this answer cost about $0.07" in html and "7 of 8 asks left today" in html
    assert rows(root)[0]["state"] == STOP and not (root / "stories" / "demo.rework.json").exists()   # an offer, no more

    r = client.post("/film/demo/rework", data={"stage": "render", "shots": SHOT, "notes": REMEDY["notes_for_agents"]},
                    follow_redirects=False)                                      # what the dialog's button sends
    assert r.status_code == 303 and "Sent%20back" in r.headers["location"]
    sent = json.loads((root / "stories" / "demo.rework.json").read_text())
    assert sent["stage"] == "render" and sent["shots"] == [SHOT] and sent["notes"] == REMEDY["notes_for_agents"]
    assert rows(root)[0]["state"] == "retry"
    assert "fixdlg" not in client.get("/film/demo").text                         # running again: the dialog is gone


def test_an_answer_is_shown_only_for_the_very_stop_it_was_asked_about(client, root):  # noqa: F811
    stop(root)
    client.post("/film/demo/fix")
    tick(root, claude())
    assert "Open the fix" in client.get("/film/demo").text
    rs = rows(root)
    rs[0].update(updated="2026-11-23T12:00:00+00:00")                            # it ran again and stopped the same way
    (root / "queue" / "stories.csv").write_text(csv_text(rs, queue.STORY_COLUMNS))
    html = client.get("/film/demo").text
    assert "Open the fix" not in html and "One shot has no video" not in html and "Ask Claude what to do" in html
    assert "7 of 8 asks left today" in html                                      # the earlier ask still counts today


def test_each_kind_of_answer_gets_its_own_button_and_none_gets_past_the_pages_checks(client, root):  # noqa: F811
    def shown(answer, state=STOP):
        stop(root, state=state)
        (root / "demo" / "reports" / "fix.json").unlink(missing_ok=True)
        client.post("/film/demo/fix")
        tick(root, claude(answer))
        return client.get("/film/demo").text
    html = shown({**ANSWER, "remedy": {**REMEDY, "kind": "redo_step", "step": "scenes"}})
    assert 'action="/film/demo/rework"' in html and '<option value="scenes" selected>' in html
    dialog = html[html.index('<dialog id="fixdlg"'):html.index("</dialog>")]
    assert 'value="render"' not in dialog                                         # the video step goes by its shots
    html = shown({**ANSWER, "remedy": {**REMEDY, "kind": "retry"}})
    assert "<b>run it again as it is</b>" in html and ">Run it again</button>" in html
    html = shown({**ANSWER, "remedy": {**REMEDY, "kind": "raise_cap", "cap_usd": 55}})
    assert 'action="/film/demo/row"' in html and 'name="budget" type="number" min="1" step="1" required value="55"' in html
    assert "Saving the cap does not start the run" in html
    html = shown({**ANSWER, "remedy": {**REMEDY, "kind": "raise_cap", "cap_usd": 55}},
                 state="blocked:estimate $42.50 + spent $1.35 is over the cap $40.00; raise the cap or reduce the plan")
    dialog = html[html.index('<dialog id="fixdlg"'):html.index("</dialog>")]
    assert 'action="/film/demo/cap-and-rerun"' in dialog and "nothing is paid until you approve it there" in dialog
    html = shown({**ANSWER, "remedy": {**REMEDY, "kind": "outside", "owner_steps": "Top up ElevenLabs.\nThen come back."}})
    assert "outside the control room" in html and "Top up ElevenLabs." in html and "It is done, run it again" in html
    html = shown({**ANSWER, "remedy": {**REMEDY, "kind": "code_change", "code_note": "The fit step stops a short."}})
    dialog = html[html.index('<dialog id="fixdlg"'):html.index("</dialog>")]
    assert "the control room cannot make yet" in dialog and "The fit step stops a short." in dialog
    assert [a for a in ("rework", "retry", "row", "cap-and-rerun") if f'action="/film/demo/{a}"' in dialog] == []
    html = shown({**ANSWER, "question": "Which shot looks wrong?", "remedy": {**REMEDY, "kind": "ask"}})
    assert "Which shot looks wrong?" in html and ">Your answer" in html and "Ask Claude again" in html
    html = shown({**ANSWER, "remedy": {**REMEDY, "shots": ["sc099_s009"]}})        # a shot this film does not have
    dialog = html[html.index('<dialog id="fixdlg"'):html.index("</dialog>")]
    assert "did not give a fix the control room can apply" in dialog and "None of the shots it named" in dialog
    assert 'action="/film/demo/rework"' not in dialog
    for action in ("approve", "reject", "shorts", "cards", "picture-remove", "look-remove"):
        assert f'action="/film/demo/{action}"' not in dialog                      # never an approval, never a removal


def test_claude_failing_and_the_daily_limit_are_said_in_the_dialog(client, root):  # noqa: F811
    import datetime
    stop(root)
    client.post("/film/demo/fix")

    def down(evidence, words):
        raise LLMError("no credit left")
    tick(root, down)
    html = client.get("/film/demo").text
    assert "Claude could not answer (LLMError: no credit left). Ask again." in html
    assert "<b>Claude could not answer</b>" in html and "Ask Claude again" in html
    today = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    record = json.loads((root / "demo" / "reports" / "fix.json").read_text())
    (root / "demo" / "reports" / "fix.json").write_text(json.dumps({**record, "history": [today] * fix.PER_DAY}))
    html = client.get("/film/demo").text
    assert "Claude was asked 8 times today for this film" in html and 'action="/film/demo/fix"' not in html
    r = client.post("/film/demo/fix", follow_redirects=False)
    assert "asked%208%20times%20today" in r.headers["location"] and not (root / "stories" / "demo.fix.json").exists()


def test_an_ask_the_runner_never_took_stops_holding_the_dialog(client, root):  # noqa: F811
    stop(root)
    client.post("/film/demo/fix", data={"words": "first"})
    name = root / "stories" / "demo.fix.json"
    ask = json.loads(name.read_text())
    name.write_text(json.dumps({**ask, "at": "2026-11-23T08:00:00+00:00"}))        # hours ago: no tick took it
    html = client.get("/film/demo").text
    assert "Claude is reading this stop" not in html and "got no answer: the runner did not pick it up" in html
    assert "Ask Claude what to do" in html
    r = client.post("/film/demo/fix", data={"words": "second"}, follow_redirects=False)
    assert "Asked%20Claude" in r.headers["location"] and json.loads(name.read_text())["words"] == "second"
    assert client.get("/film/demo").text.count("Claude is reading this stop") == 2


def test_asking_again_shows_claude_what_it_said_last_time(client, root):  # noqa: F811
    stop(root)
    client.post("/film/demo/fix")
    tick(root, claude({**ANSWER, "question": "Which shot looks wrong?", "remedy": {**REMEDY, "kind": "ask"}}))
    assert "Your earlier answer" not in json.dumps(json.loads((root / "demo" / "reports" / "fix.json").read_text()))
    client.post("/film/demo/fix", data={"words": "the first one, the ferryman looks like a boy"})
    ev = json.loads((root / "stories" / "demo.fix.json").read_text())["evidence"]
    assert "Your earlier answer about this same stop" in ev and "You asked the owner: Which shot looks wrong?" in ev
    assert "You proposed: ask" in ev
    seen = []
    tick(root, claude(seen=seen))
    assert seen[0][1] == "the first one, the ferryman looks like a boy"
    assert "remake the shots it names" in client.get("/film/demo").text


def test_whatever_claude_writes_is_shown_as_text_never_as_page_code(client, root):  # noqa: F811
    stop(root)
    client.post("/film/demo/fix")
    nasty = '<script>alert(1)</script><form action="/film/demo/approve">'
    tick(root, claude({**ANSWER, "what_happened": nasty, "why": nasty, "after": nasty,
                       "remedy": {**REMEDY, "notes_for_agents": nasty + '</textarea>'}}))
    html = client.get("/film/demo").text
    dialog = html[html.index('<dialog id="fixdlg"'):html.index("</dialog>")]
    assert "<script>alert(1)" not in dialog and "&lt;script&gt;alert(1)&lt;/script&gt;" in dialog
    assert 'action="/film/demo/approve"' not in dialog and "&lt;/textarea&gt;" in dialog
