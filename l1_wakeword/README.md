# L1 — wake-word listener

Continuous "Hey Jarvis" detection via [openWakeWord](https://github.com/dscripka/openWakeWord).

## How to turn this off

Put first because someone reaching for this section is usually in a hurry.
Three ways, all of them work:

- **`kill <pid>` (or Ctrl-C if running in a terminal).** Stops it and it
  stays stopped — `daemon.py` catches the signal and exits cleanly, and
  the LaunchAgent's `KeepAlive` (`SuccessfulExit: false`) only restarts on
  a *crash*, not a clean exit. If this is running as the LaunchAgent, it
  won't come back on its own after this.
- **`./stop_wakeword.sh`.** The documented method — same effect as `kill`
  plus it fully unloads the LaunchAgent (`launchctl bootout`), so it also
  won't come back on the next login/reboot until reloaded.
- **Confirm it's actually off:** `launchctl list | grep jarvis` — no
  output means it's not running and not registered to start.

Full reasoning for why `KeepAlive` is configured this way (and why an
earlier version of this file got it wrong) is in "Process lifecycle"
below.

## Why openWakeWord, not Porcupine

Porcupine (Picovoice) was the originally-planned engine. Checked live before
installing anything: Picovoice discontinued their Free Tier, existing Free
Tier AccessKeys stop authenticating June 30 2026, and there is no
non-commercial tier planned (their own stated reason: "focusing on our core
business, enterprise deployments"). The SDK will not run at all without a
valid key. That is a dead option for a personal project, not a
soon-to-expire one — by the time this was checked (Aug 2026) it already was.

openWakeWord is Apache-licensed, fully local, no account/key/network call,
and ships a pretrained `hey_jarvis_v0.1` model out of the box — matches our
wake word without custom training. One license note: the *pretrained model
weights* (not the code) are CC-BY-NC-SA 4.0 due to training-data provenance.
Fine for this non-commercial single-user tool; revisit if that ever changes.

## Setup

```
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python fetch_models.py     # downloads hey_jarvis + preprocessor models into ./models
```

## Usage

```
.venv/bin/python listener.py                    # live mic, prints one JSON line per detection
.venv/bin/python listener.py --file clip.wav     # offline scoring of a 16kHz mono wav
```

## Measured false-trigger behavior (synthetic `say` audio, small sample — real-voice validation still pending)

The model requires the *cadence* of "hey [name]", not just the word "Jarvis":

- Saying "Jarvis" alone, mid-sentence, with no "hey" — **no detection**, tested
  across 5 varied phrases including ones that talk *about* the Jarvis system
  ("I think Jarvis needs a better wake word detector"). This matters because
  Ayman is expected to reference the system by name during long dictations.
- "Hey Jarvis" fires reliably (0.87–0.999 confidence) and the model **does
  re-fire on a second "hey jarvis" later in the same continuous stream**,
  with score dropping back below threshold in between — supports the
  toggle start/stop design (same keyword twice), pending real debounce logic
  in the state machine that consumes these events.
- **Real false-positive found:** "Hey Travis, can you check on that?" fires
  the hey_jarvis model at 0.82–0.99 confidence. The model is keying on the
  "hey + name" phonetic pattern more loosely than just "Jarvis" specifically.
  This is a genuine risk, not a theoretical one — needs to be part of the
  real-voice validation pass, and if it holds up, is a reason to reconsider
  same-keyword toggle vs. a second distinct stop phrase.

Every wake word/melspectrogram/embedding model is `*.onnx` and gitignored —
`fetch_models.py` is how to reproduce `./models` from a fresh checkout.

## daemon.py — the state machine

Ties the wake-word listener, the VAD chunker, and the whisper daemon into
one running loop. One process, one mic, three mutually exclusive states:
`IDLE`, `CAPTURING`, `CANCEL_ARMED`.

**Stated invariant, per the Lead's ruling that this needed to be
verifiable by reading, not inferred from code structure:**
**"Hey Jarvis" has exactly one meaning at any moment, determined by
system state — never two.** This is implemented as **exclusive claim**,
not parallel-with-suppression: there is one state variable and one
detector consumer at a time. `CANCEL_ARMED` is only entered via L4's
`listen_for_cancel` RPC, which only happens after a dictation has already
been handed off and the daemon is back in `IDLE` — so a "Hey Jarvis"
during a cancel window cannot also be interpreted as "start a new
dictation," because there is no code path evaluating that interpretation
while `CANCEL_ARMED` is active. Boundary case (a detection arriving right
as the window's timeout expires) resolves toward cancel, per the same
asymmetry as the threshold decision below — the RPC handler reports
whatever the detector said before the timeout fired, not "whichever
happened to run last."

**Two detection thresholds, not one, and why:** `IDLE_THRESHOLD` (0.5) for
starting a dictation, `RECOVERABLE_THRESHOLD` (0.3) for *ending* one
(post-guard stop-toggle) and for the cancel window. This isn't
speculative — a real synthetic test clip's closing "Hey Jarvis." peaked
at 0.348, comfortably below 0.5 but above 0.3, meaning the un-lowered
threshold would have missed the stop trigger entirely and run the
dictation to the (also real, separately tested) 50s silence safety net
instead of ending when Ayman actually stopped talking.

The asymmetry argument ("false positive is cheap, false negative is the
failure being guarded against") is NOT equally true for both lowered
cases, and this was checked rather than assumed: a spurious *cancel* is
fully recoverable (re-dictate), but a spurious *stop* truncates a real
dictation mid-thought before it's caught by `confirm_plan`. Measured the
actual risk before deciding whether to split the thresholds: 20 diverse
synthetic "ordinary conversation" sentences (no wake-word-like phrasing)
scored a **max of 0.002** against this model — nowhere near either
threshold. The one real false-positive risk found this session ("Hey
Travis," 0.82–0.99) is already well above 0.5, so a lower threshold
doesn't change that risk either. Conclusion: no evidence to split the
thresholds despite the worse failure mode on the stop side — the
measured FP rate at 0.3 is ~zero on realistic content, so lowering it
costs nothing.

**Stop-word audio handling, and two more bugs this surfaced:** the
pending (not-yet-VAD-cut) audio when the stop word fires can't simply be
transcribed as-is — "Hey Jarvis" would show up verbatim at the end of the
transcript (confirmed, this happened before the first fix) — but it also
can't simply be discarded wholesale, confirmed by testing the case where
Ayman says the stop word with **no pause** before it: an entire
multi-instruction dictation ("...also tell Ship Check to redeploy, Hey
Jarvis," no pause anywhere) was silently lost in full, because the whole
thing was still one undischarged VAD chunk with nothing marking where
real content ends and the stop phrase begins.

Fixed with `StreamingChunker.pop_trimming_tail`: trims a fixed
`WAKE_WORD_TAIL_TRIM_S` (1.0s) off the end of the pending buffer — sized
from every "hey jarvis" detection observed this session spanning roughly
0.5–0.6s of above-threshold scoring, with margin for the quieter lead-in.
This alone reintroduced a different bug: when there *was* a pause before
the stop word, the pending buffer is mostly that pause's silence plus a
short wake word, and trimming a fixed tail off the end can leave a
near-silent remainder — which Whisper doesn't return nothing for, it
**hallucinates** ("TV Gelderland 2021" out of what should have been an
empty remainder, confirmed by testing). Fixed by tracking per-frame
speech/silence classification and requiring the trimmed remainder to
contain at least `_MIN_SPEECH_FRAMES_IN_REMAINDER` (~160ms) of actual
speech before sending it to Whisper at all.

A third bug, also found by testing the no-pause case specifically: a
single spoken "Hey Jarvis" scores above threshold across several
consecutive frames, so the same utterance that just fired the STOP
transition was crossing `IDLE_THRESHOLD` again on the very next frame and
immediately opening a new, unintended dictation — `CAPTURING -> IDLE ->
CAPTURING` within 40ms of simulated time. Fixed with
`POST_STOP_COOLDOWN_S` (1.5s): wake-word starts are ignored for this long
after any dictation ends.

All three fixes verified together against the original pause-before-stop
clip (still clean, no regression), the no-pause-with-filler-content clip,
and the no-pause-with-real-instruction clip (full instruction now
recovered, previously lost in full). The trim/cooldown durations are
heuristics sized from synthetic `say` timing — real-voice validation
should confirm them against how Ayman actually speaks, not just assume
these numbers hold.

**Two-layer hallucination defense, because the "TV Gelderland 2021"
finding above generalizes.** That wasn't a one-off: Whisper doesn't
return empty on low-speech audio, it confabulates, and any confabulated
sentence that reaches L3 becomes a routed instruction with no signal
that Ayman never said it. Fixed at two layers, deliberately not just one:

1. **`MIN_SPEECH_FRAMES_PER_CHUNK` is now enforced on every chunk, not
   just trimmed remainders.** Moved into `StreamingChunker._gate`, which
   every emission path (silence-cut, maxlen-cut, flush, trimmed-tail)
   routes through — same "class invariant, not convention" reasoning as
   the VAD-advance-once-per-frame fix. Re-ran the original "TV
   Gelderland 2021" scenario after this change: the chunk is now dropped
   *before Whisper is ever called* (`vad_chunker: dropped trimmed-tail
   chunk (0.73s, only 0 speech frames)`), not caught after the fact.

2. **`../l2_transcription/hallucination_filter.py` — a second layer for
   whatever passes the VAD gate but is still confabulated** (background
   noise the VAD classified as speech, etc.). Two independent signals:
   a pattern list of Whisper's well-documented subtitle-corpus
   confabulations ("thanks for watching," "subtitles by," channel/credit
   patterns), and a structural n-gram repetition check (a 1-4 word
   phrase repeating 4+ times consecutively — degenerate decoding loops
   repeat phrases as often as single words, so this checks both). Every
   drop is logged with the reason and the dropped text, not silently
   swallowed, so the false-drop rate is observable rather than assumed.
   Wired into `daemon.py` at the one place a chunk's audio becomes
   transcript text (`DictationSession._transcribe_and_append`), so
   every caller gets it structurally rather than needing to remember.

Explicitly not claiming full coverage: the pattern list catches known,
previously-reported hallucination phrasings, not arbitrary novel ones —
that's what layer 1 (the VAD gate) and the repetition check are for,
since neither depends on Whisper's output matching something already
seen. Fails toward dropping throughout, per the same asymmetry as
everything else on this project: a dropped real sentence costs a
re-dictation Ayman will notice missing at the plan-confirmation summary;
a hallucinated one that gets through is silent and can't be un-delivered.

**Bug found and fixed during this build, logged here because it's the
kind of thing worth being able to point to later:** an earlier version
re-ran the VAD chunker over the *entire* accumulated buffer on every new
frame, using the same stateful `SileroVAD` instance whose LSTM hidden
state had already advanced past audio it was now being asked to re-score
from the start. Produced garbage transcripts ("So", "Aw!", "Hey!"
instead of real sentences) — caught by testing against a real
multi-sentence clip, not by inspection. Fixed by extracting
`StreamingChunker` (`../l2_transcription/vad_chunker.py`), which
enforces "the VAD advances exactly once per frame, ever" as a class
invariant rather than a convention callers have to remember. **This bug
was contained entirely to new, previously-unreported daemon.py
integration code — it never touched `whisper-cli`/`whisper-server`
directly, so it does not affect the benchmark numbers, the q5_0 model
pick, or the warm-latency measurements already reported.** Those used
separate, already-tested code paths.

A second, related bug from the same root cause (real-vs-simulated time):
the 3-second start guard used `time.monotonic()` internally, which is
wall-clock time — harmless live, but meaningless in `--simulate` mode,
which processes 10+ seconds of audio in a fraction of a real second, so
the guard never actually lifted during a simulated run. Fixed by making
`DictationSession` take an explicit `now` parameter instead of sampling
the clock itself, so simulated-time and wall-clock-time callers get
identical behavior.

## Wake-word detection sensitivity: one mechanism explaining three findings

Discovered while running the full-length (~5 min) pipeline test and chasing
why the closing "Hey Jarvis" wasn't detected. Took three rounds of
self-correction to get right — recording the wrong turns too, since they're
part of why the final conclusion is trustworthy.

**The single governing principle: a confident utterance carries enough
signal margin that unfavorable conditions can't push it under threshold; a
marginal one doesn't.** Everything below is a consequence of that.

**What it isn't (retracted after testing):**
- *Not* weak TTS prosody on one specific phrase — the first hypothesis,
  killed by finding a frame-alignment bug in the test that produced it.
- *Not* cumulative stream duration ("5 minutes of accumulated state"). Measured
  directly: a curve across 0-283s of prior audio, frame phase held constant
  via exact-512-sample-boundary truncation at every point, scored
  **0.455, 0.841, 0.343, 0.943, 0.212, 0.951, 0.325, 0.684, 0.474, 0.627**
  at 0/30/60/90/120/150/180/210/240/270s. No relationship to elapsed time —
  a real duration-based mechanism wouldn't look like that.

**What it is:** sensitivity to the audio in roughly the **last 1-3 seconds**
before the phrase. Confirmed by holding cumulative duration at zero and
varying only local context: 1/2/3/5/10s of real recent audio (no earlier
stream at all) reproduced the *same* wild variance (0.246-0.933) as the
283s "cumulative" test. Silence immediately before scores highest measured
(0.94-0.97); speech immediately before is variable, sometimes badly
suppressive.

**This explains three separate findings under one cause:**
1. The original stop-word miss (0.214) — a long dictation's closing phrase,
   in `say`'s rendering, happened to follow speech-like content rather than
   a clean pause.
2. Alignment sensitivity — a 24ms frame-grid shift moved the same marginal
   phrase from 0.323 to 0.168, a ~2x swing from windowing alone, nothing to
   do with the audio.
3. TTS-interrupt safety (tested per the Lead's ask, see below) — didn't
   reproduce, because the tested cancel phrase was confident-baseline, not
   marginal.

**Why the toggle design was already right, now for an understood reason
rather than luck:** the design asks for a deliberate pause before the wake
word. A pause is exactly the condition — silence immediately before —
that scores highest. Not a coincidence in retrospect.

## Fix 1 (built): multi-offset ensemble for the cancel window

`CANCEL_ENSEMBLE_OFFSETS = [0, 171, 341]` in `daemon.py` — three fresh
detector instances per cancel-window arm, frame grids staggered by ~1/3 of
the 512-sample hop each, max score taken across them. Addresses alignment
luck specifically: measured, the ensemble max recovered the 0.323 case (a
single arbitrary offset could have landed as low as 0.168 on the identical
audio). Scoring logic (`fresh_ensemble`/`score_frame_ensemble`) is built
and unit-testable without a mic; wiring it to the live audio source is the
one remaining TODO, marked in code, blocked on the live-mic session.

## Fix 2 (considered, NOT built): rolling detector rotation

Was the Lead's proposed fix for "cumulative stream duration" suppression —
correctly cancelled once the duration curve above showed that mechanism
doesn't hold. A rotation scheme would have been real code, shipped, and
defended in this README, mitigating something that isn't happening — and
critically, a freshly-rotated instance still meets the same unfavorable
*local* context right before the phrase, so it wouldn't have caught the
original 0.214 case anyway. Documenting the near-miss because "we built a
fix for the wrong model of the problem" is a failure mode worth naming,
not just avoiding quietly.

## TTS-interrupt safety test (the scenario that matters most)

The cancel window opens right after the system finishes speaking a plan
summary through the speakers — if the mic picks up that TTS (it will,
speakers on), the worst case is Ayman interrupting mid-summary to cancel,
meaning his "Hey Jarvis" is preceded by *our own active TTS* with zero gap.
Tested directly: a synthesized ~11.4s plan summary, with a confident
"Hey Jarvis, cancel." spliced in at **9 different points** — mid-summary
interrupts at 15/35/50/60/75/95% through, plus 0ms/300ms/1000ms gaps after
the summary ends. **Every condition scored 0.999-1.000.** No exceptions.

Consistent with the governing principle above: "Hey Jarvis, cancel." is a
confident-baseline phrase (0.965-0.999 fresh, same range as every clean
opener tested this session), so it carries enough margin that TTS bleed-
through in the last 1-3s doesn't suppress it below threshold. TTS
interference is not a measured risk for a normally-spoken cancel.

**The residual risk that IS real, per the same principle:** a *quiet or
half-hearted* cancel — marginal-baseline, the way the earlier stop-word
and alignment tests were — could plausibly be suppressed the same way a
weak phrase was throughout this investigation. Not tested directly (would
need a genuinely quiet synthetic rendering, and the variance seen today
across "controlled" synthetic conditions means that test likely wouldn't
be conclusive either) — flagged for the live-mic session instead.

**Design recommendation for Ayman, not implemented here (his call, not
mine — Lead is raising it):** widen the cancel window from ~2.5s to ~4-5s.
Reasoning: if a quiet first attempt is missed, there's currently no room
for a firmer second try within the window — the failure mode is "quiet
cancel missed, no second chance, delivery proceeds." A wider window turns
an unrecoverable miss into a recoverable one, at a cost of 1.5-2.5s added
to a loop that's already ~90s and dominated by orchestrator reasoning —
close to invisible in context. The original 2.5s was chosen when the
design read back a full transcript rather than a plan summary; worth
re-deciding with what's now known about detection risk, not treating as
already settled.

## Real-voice validation items from this investigation (live-mic session)

- Whether real human speech reliably produces the helpful pre-phrase
  silence the way `say`'s rendering does when a deliberate pause is taken.
- Whether a rushed/quiet real cancel behaves like the marginal synthetic
  phrases tested here (the one residual risk not resolved by synthetic
  testing — variance was too high in "controlled" conditions to trust a
  synthetic quiet-speech test as conclusive).
- The cancel-window-width recommendation above, pending Ayman's decision.

```
.venv/bin/python daemon.py --simulate clip.wav     # drives the full state machine over a pre-recorded file
```

**Not yet live-mic tested, and won't be without Ayman's explicit
go-ahead and presence** — per the standing rule established after an
unrelated incident during this build (a different component's test
produced audible TTS output unexpectedly): no unattended microphone
capture, ever, live-mic testing only while actively watched and stopped
when watching stops. `daemon.py`'s live-mic branch currently just prints
that it isn't wired up and exits — this is deliberate, not an oversight.
The cancel-socket server (`~/.jarvis/l1.sock`) is scaffolded
(`cancel_socket_server()`) but its actual detection logic is a documented
placeholder for the same reason.

## Process lifecycle: launchd, TCC, and how to turn this off

This was part of the original L1 assignment (survive reboot/sleep, stable
mic-permission identity) and got dropped when the architecture pivoted
twice before it was built — flagging that gap plainly rather than
backfilling it, since it should have been called out as "still owed" in
an earlier status update instead of going unmentioned.

**What exists:** `com.jarvis.l1wakeword.plist` (a launchd LaunchAgent,
`RunAtLoad` + `KeepAlive`), `run_daemon.sh` (the stable entrypoint the
plist points at), `install_launch_agent.sh` (copies the plist into
`~/Library/LaunchAgents/`), `stop_wakeword.sh` (the real off switch).

**Installed but deliberately not loaded.** Per the standing no-unattended-
mic rule: a LaunchAgent that auto-starts a microphone listener on login is
exactly the case that rule exists for. `install_launch_agent.sh` copies
the plist and prints the `launchctl bootstrap` command rather than running
it. First load happens during the live-mic session, with Ayman present —
that session also answers the one open design question below, which
should be resolved before this runs unattended across reboots.

**How to turn it off, once it's running:** `stop_wakeword.sh`. Plain
`kill`/`pkill` will NOT work — `KeepAlive` is bare `true` (survives any
exit, clean or crashed), specifically so there's one unambiguous "off"
(`launchctl bootout`, wrapped by the script) instead of a kill-vs-launchctl
question about what "stopped" even means. This is the answer to "how does
Ayman turn it off without reading source."

**The open question: what does TCC actually key the microphone-permission
grant on, and where should `run_daemon.sh` point?**

`run_daemon.sh` currently execs
`/opt/homebrew/opt/python@3.13/bin/python3.13` — Homebrew's floating
"current version" alias, not the venv's own python. Verified, not assumed:
`.venv/bin/python3.13` is a symlink chain resolving to a real binary under
`/opt/homebrew/Cellar/python@3.13/3.13.7/...`, and `execve()` always
resolves symlinks before running — so recreating the venv doesn't change
what actually executes or opens the mic. The real exposure is a
`brew upgrade python@3.13`, which moves the Cellar version directory.

Since invoking that alias directly bypasses Python's normal venv
auto-detection (which walks from the invoked path looking for a sibling
`pyvenv.cfg` — the alias's directory has none), `run_daemon.sh` sets
`PYTHONPATH` explicitly to the venv's `site-packages` rather than relying
on implicit activation. Verified working (no mic needed to check this
part): `PYTHONPATH=.../site-packages /opt/homebrew/opt/python@3.13/bin/python3.13`
imports `numpy`/`onnxruntime`/`openwakeword`/`sounddevice` correctly.

**What's still open, and can't be closed without the mic:** the Lead's
read, which stands as the better one — both the Homebrew alias and the
fully-resolved Cellar path share a defect regardless of which one TCC
actually uses: **they point at a binary this project doesn't own.**
Homebrew can move or replace it on any upgrade, and the failure mode is
silent (Ayman talks to a machine that quietly stopped listening because
macOS re-prompted for a permission nobody was there to grant). Two better
options, not yet built, pending which one the live-mic test's answer
requires:

- **(a) Package as a minimal `.app` bundle.** TCC keys the grant on
  bundle identifier + code signature, not a raw executable path — the
  only option that's stable by construction rather than by luck. Also
  gives Ayman a recognizable entry in System Settings → Privacy →
  Microphone, which matters for the visible-off-switch trust point above.
- **(b) Vendor a standalone interpreter under `~/.jarvis/runtime/`.**
  Cruder, less work than (a), removes Homebrew from the trust chain by
  just owning the binary outright.

The live-mic session is where this actually gets answered — whether TCC
re-prompts across an interpreter path change is an empirical question
that needs a real permission grant and a real trigger event to test, not
something reasoning from documentation can settle. That session should
also cover: idle CPU/battery measured over a real stretch (not claimed),
confirming `KeepAlive` genuinely survives a sleep/wake cycle, and
verifying macOS's orange menu-bar mic indicator actually appears while
this is listening. If it does, that's a real always-on trust signal we
get for free and should be documented as the at-a-glance way to know
whether Jarvis is live — if it somehow doesn't, that's worth knowing
too, since the visible-indicator point above currently rests on
`stop_wakeword.sh` and process-list checks alone. All three need the
listener actually running, so one session covers them instead of three
separate interruptions.
