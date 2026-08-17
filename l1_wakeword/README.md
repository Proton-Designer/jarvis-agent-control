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
  This held up under further testing (see "THE GOVERNING ASYMMETRY" below)
  and is exactly why the same-keyword toggle was reconsidered: "hey jarvis"
  remains the start trigger (now with two-stage verification added), but
  stopping a dictation switched to a distinct, transcript-matched phrase
  ("that's it," see "Stop phrase" below) instead of a second acoustic
  "hey jarvis," sidestepping this risk entirely for the stop side.

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
starting a dictation, `RECOVERABLE_THRESHOLD` (0.3) for the cancel window.
**Update: `RECOVERABLE_THRESHOLD` is cancel-only now.** It used to also
gate *ending* a dictation (a post-guard acoustic "hey jarvis" stop-toggle)
until the stop phrase was reworked to match transcript text instead (see
"Stop phrase" below) — measured the same way as everything else on this
project: 20 diverse synthetic "ordinary conversation" sentences scored a
**max of 0.002** against this model, nowhere near 0.3, so the cancel
window's false-positive rate on realistic content is ~zero. The one real
false-positive risk found this session ("Hey Travis," 0.82–0.99) is
already well above `IDLE_THRESHOLD`, so lowering the cancel threshold
doesn't worsen that risk either.

## THE GOVERNING ASYMMETRY: start fails closed, stop/cancel fail open

Discovered why this needs to be a stated rule, not left implicit, during
live-mic testing: **ordinary two-syllable human names can score
indistinguishably from a genuine "Hey Jarvis" on the acoustic model
alone.** Measured directly, same voice, same batch: "Hey Charles" = 0.998,
"Hey Travis" = 0.983, against a genuine "Hey Jarvis" baseline of 0.999.
Not marginal — **no threshold separates 0.998 from 0.999.** Voice-
assistant phrases ("Hey Siri," "Hey Google," "Hey Alexa," "Hey Cortana,"
"Hey Meta") were tested too and are completely clean (0.000 across every
voice) — the risk isn't assistant-phrase confusion, it's the "Hey
[two-syllable name]" prosodic shape generally, which ordinary names can
share by chance.

This is why the two lowered-threshold decisions above (stop, cancel) do
**not** generalize to the start trigger, and why that's correct rather
than inconsistent:

- **Stop/cancel fail open** (biased toward accepting) because a false
  negative there is recoverable — repeat the phrase.
- **Start fails closed** (biased toward rejecting) because a false
  *accept* opens the microphone and transcribes whatever is said next in
  the room. Measured, not hypothetical: this happened during live testing
  (see below). Not recoverable the way a missed stop is.

**Fix: a second-stage verification pass, `verify_wake_trigger()`, gates
every IDLE -> CAPTURING transition.** On an acoustic trigger, the ~2s
pre-roll+trigger buffer is sent to the already-warm whisper-server; the
transition only proceeds if the transcript recognizably contains "jarvis"
(permissive on spelling — "Jarvis"/"jarvis,"/"Jervis" all accepted,
case-insensitive). Empty, ambiguous, or wrong-name transcripts reject and
stay IDLE. Every rejection is logged with the score and the transcript so
the false-rejection rate is measurable over time, not assumed.

**Why stacking these two checks works instead of being redundant:** the
acoustic model scores *prosodic shape* (why "Hey Charles" fools it).
Whisper transcribes *phonetic content* (where "Charles" and "Jarvis" are
nothing alike). The two failure modes are uncorrelated — a phrase that
fools one has no particular reason to also fool the other.

**Verified end to end, not just designed:** re-ran the full 35-clip
distractor set (major assistant phrases + 32 phonetically-varied "Hey
[name]" constructions, 3 voices) through the actual two-stage pipeline.
**Both acoustic false-starts (Hey Charles, Hey Travis) were rejected 100%
of the time** ("REJECTED by verification (heard: 'Hey Charles')"), while
**all 3 genuine "Hey Jarvis" calibration clips (3 voices) verified
correctly and opened CAPTURING.** 0/32 distractors survive to a false
capture; 3/3 genuine triggers still work. This isn't "reject everything"
— it discriminates correctly in both directions.

**Cost:** one synchronous whisper-server call (~0.5s measured) per
trigger evaluation, including rejected ones. Paid once at the very start
of a dictation that then runs for minutes — invisible in context, same
conclusion as every other latency measurement this session. In live mode
this blocks the frame-processing loop for that ~0.5s; deliberate, not an
oversight — nothing else needs the event loop during that window
(verification only runs transitioning into a new dictation, never
mid-capture or mid-cancel), so it wasn't worth the complexity of moving
it to an executor.

**Checked directly whether that block drops audio, given Ayman is
expected to keep talking through it** ("Hey Jarvis, tell the API
gateway..." as one continuous utterance, no pause — precisely the no-
pause case documented earlier in this file). It doesn't, and this was
verified rather than assumed: `asyncio.Queue` + `call_soon_threadsafe`
don't drop scheduled callbacks during a synchronous blocking stretch,
they queue; the next `await` point (right after `on_frame()` returns,
by which time state has already flipped to CAPTURING) drains them in
order into the now-existing session. Verified with a real-time-paced
test of the actual `LiveController`/`on_frame`/`verify_wake_trigger`
code (not a simplified stand-in) against "Hey Jarvis, tell the API
gateway to run its test suite and check the response time logs" with
zero pause: transcript came back complete, nothing clipped off the
front. **What this does NOT cover:** the OS/PortAudio hardware buffer
underneath `sounddevice` — that layer requires a real microphone to
test and is on the live-mic session list, not claimed as verified here.

**Documented fallback, not built:** a custom-trained "Jarvis"-only
openWakeWord model (openWakeWord supports this, ~1hr per their docs).
Not needed while verification gets false starts to zero on tested cases —
if that changes with real-voice data, this is the next lever. A custom
model could additionally be **speaker-specific** (trained on Ayman's
voice specifically), which would mean media playback and other people's
voices don't trigger detection *at all*, not just get caught downstream —
a real advantage if verification alone ever proves insufficient.

## Stop phrase: matched on transcript text, not acoustics

Ayman ends a dictation by pausing and saying **"that's it"** instead of a
second "Hey Jarvis." Deliberately not a second acoustic wake-word model:
`daemon.py`'s `match_stop_phrase()` checks whether a **completed chunk's
Whisper transcript** ends with an accepted variant
(`STOP_PHRASE_VARIANTS = {"that's it", "thats it", "that is it"}`, since
Whisper renders the same spoken phrase with varying
punctuation/capitalization/contraction-expansion) — not a second score
from the acoustic model. This sidesteps essentially every acoustic problem
documented in this file: marginal-score suppression, frame-alignment
luck, and the "Hey Charles"/"Hey Travis" cross-trigger risk are all
properties of scoring *prosodic shape* against a small trained model, and
none of them are a property of checking whether text ends with a phrase.

Two conditions, both required, both structural rather than heuristic:

1. **Only matched at the very end of the chunk's transcript.** "That's it
   for the gateway, now let's also redeploy the mobile API" does not
   match, and is inherently safe regardless of wording — more speech in
   the same chunk means the VAD never cut there, so there was no pause to
   treat as an ending. Verified directly with a synthetic clip built for
   exactly this case: the mid-sentence "that's it" was transcribed and
   kept as ordinary content, the dictation only ended on the later,
   deliberate, silence-closed "That's it."
2. **Only matched on a chunk that closed because of a detected pause**
   (`StreamingChunker.last_cut_reason == "silence"`), never one forced by
   the 30-second hard cap (`"maxlen"`) — a maxlen cut can land anywhere,
   including mid-word, so a coincidental match there wouldn't mean Ayman
   actually stopped.

**This also deletes the no-pause-before-stop problem instead of solving it
differently.** The old acoustic mechanism (see the incident section below)
needed a fixed-duration audio trim (`pop_trimming_tail`) specifically to
handle Ayman saying the stop word with no pause before it, because audio
alone can't tell where real content ends and the wake word begins.
Text-based stripping handles the same case for free: `match_stop_phrase`
strips the matched phrase's word count off the end of whatever text the
chunk transcribed, so "...also redeploy that's it" (no pause, one chunk)
correctly reduces to "...also redeploy" — no separate heuristic, no
duration to size and re-validate against real speech. Verified directly:
a no-pause synthetic clip ("Also tell ShipCheck to redeploy that's it.")
produced the transcript `"Also tell ShipCheck to redeploy,"` with the stop
phrase cleanly removed.

**Also removed as a direct consequence, not separately:** the 3-second
start guard (`GUARD_S`/`in_guard`) and the post-stop cooldown
(`POST_STOP_COOLDOWN_S`) both existed to patch problems specific to
*reusing the acoustic wake-word detector* for the stop trigger (a repeat
"Hey Jarvis" immediately re-triggering, or the guard against ending a
dictation too soon after it opened). Neither problem exists once stop is
text-matched instead, so both were deleted rather than left in place
unused — see the incident section below for what they were protecting
against and why that's now moot.

"Hey Jarvis" remains the START trigger, unchanged — still acoustic, still
gated by the two-stage verification above. **Cancel stays acoustic too, a
deliberate asymmetry, not an oversight:** the cancel window doesn't chunk
speech into transcripts the way a dictation does (it's a short fixed
confirm window, not ongoing capture), so there's no transcript to match
against — the acoustic ensemble detector (below) is what's actually been
measured working there (0.999 on the TTS-interrupt test), and stays.
**Stop and cancel now use genuinely different mechanisms** — worth stating
explicitly so nobody assumes symmetry between the two just because they
used to share one acoustic model.

## The incident that motivated this: real ambient audio during live-mic testing

**Historical — describes the acoustic stop-word mechanism in place at the
time (fixed-duration audio trim, start guard, post-stop cooldown), since
replaced entirely by the transcript-based stop phrase above.** Kept
because it's the reasoning trail that explains *why* the mechanism
changed, and the ambient-audio incident and hallucination-defense findings
below remain fully current — none of that was specific to the old stop
mechanism.

First live-mic run (agent-supervised, offline/no-audio-produced by the
agent at the time) triggered twice within 20 seconds on real audio in the
room — scores 0.829 and 0.988, the second with coherent, sentence-
structured transcribed content. Two hypotheses were raised and both were
checked against evidence and refuted (a same-machine TTS source, ruled
out by timestamp — the logged audio was 37 minutes earlier; a concurrent
real-voice test, ruled out because none was running) before landing on
the explanation that held up: the cross-trigger risk documented above.
Not proven to be the specific cause of that incident, but a specific,
testable, statistically strong candidate, unlike the two ideas that came
before it and didn't survive checking.

Handling at the time: stopped the live-mic process immediately, deleted
the captured transcript content without repeating or retaining it
(the diagnostic scores/timestamps were kept — non-sensitive, needed to
investigate), and held all further live-mic testing for explicit
re-authorization. That's the standing procedure for anything like this
going forward, not a one-off response.

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

## Known limitation: verification checks acoustic content, not addressee

A second real capture, from Ayman's first authorized live-mic session
(agent-supervised by gu2s6tnt directly, not this agent): the wake word
fired on ambient conversation between Ayman and someone else, and the
transcript began *"That's true. What's up? We're creating Iron Man IRL
Iron Man Jarvis, Dino, Dino Transcripted."* Two-stage verification did
**not** reject this — correctly, by its own definition: they genuinely
said "Jarvis," which is exactly what `verify_wake_trigger()` checks for.

**The gap this exposes:** verification distinguishes *saying the word
"Jarvis"* from *saying something that merely sounds like it* (the "Hey
Charles" problem above). It cannot distinguish *saying the name* from
*addressing the system* — those are different questions, and only the
first one has a per-utterance acoustic/phonetic signal to check. No
change to `verify_wake_trigger()` fixes this; it would need to be solved
one layer up, with actual dialogue-act/addressee understanding, which is
out of scope for L1.

**Where the real fix lives, and why it changes the consequence rather
than the mechanism — shipped, not just planned.** The gap itself is
unchanged: L1's verification still can't tell "said the name" from
"addressed the system," and no version of `verify_wake_trigger()` fixes
that on its own. What changed is what a false trigger now *costs*.

The L2.5 concierge (`l2_5_concierge/`) already suppresses speech on any
CHAT-classified turn — see `concierge.py`'s guard — so the audible-
interjection risk this entry originally worried about (Jarvis speaking an
opinion into a private conversation) is closed regardless of anything
below. What remained open was retention: a false trigger still wrote a
full transcript of whatever was overheard to
`~/.jarvis/dictations/`, permanently, with no expiry.

`classifier.assess_retention()` (called only for CHAT-classified
transcripts — a transcript naming a live session structurally can't reach
CHAT at all, see the hard rule above) now makes that call, on gu2s6tnt's
original discriminator (credited) plus one more independent check:

1. A deterministic imperative-pattern check ("can you...", "please
   check...", a bare action verb) — if it fires, the transcript is kept
   regardless of anything else. Cheap, over-inclusive on purpose (a false
   positive here just means an ambient remark gets kept, not discarded,
   which is free).
2. If that doesn't fire, one model call decides ADDRESSED / AMBIENT /
   UNSURE — deliberately three-way so "genuinely unclear" is its own
   answer, not a coin flip between the other two.

Only `AMBIENT`, with no imperative match, discards. Everything else —
including a plain `UNSURE` — keeps the file. This is the one place the
project's usual fail-toward-dispatch asymmetry inverts: forwarding an
ambiguous turn to L3 costs nothing but time and is fully recoverable;
discarding a transcript is not, so the bar for discarding is deliberately
higher than the bar for every other decision in this system.

**Found and fixed during testing, worth recording because it's the kind
of thing that's easy to validate against the wrong input shape:** the
addressee judgment was measurably unstable to capitalization alone —
`"Thanks, appreciate it."` (capital T) verdicted AMBIENT, while the
semantically identical `"thanks, appreciate it"` (lowercase) verdicted
UNSURE, reproducibly, every time. Whisper *always* capitalizes sentence
starts, so every real transcript would have hit the capitalized, more
discard-prone path — a synthetic lowercase test string would have looked
fine while the real input shape didn't. Fixed by lowercasing before the
model call, verified against the exact case that exposed it plus the
real "Iron Man" transcript (still correctly discards) and genuinely-
addressed capitalized cases (still correctly keep).

Discard events are logged (event, timestamp, wake-word score,
classification, character count) to `~/.jarvis/latency_log.jsonl` for
measuring the real false-trigger rate — never the transcript content.
What a false trigger now costs: from *persists a transcript of Ayman's
private conversation to disk permanently* to *briefly opens the mic,
finds nothing addressed to it, and forgets it*.

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
- **The transcript-based stop phrase ("that's it") has only been tested
  against synthetic `say` audio so far** (three cases: pause-before-stop,
  no-pause-before-stop, and the mid-sentence false-positive case — see
  "Stop phrase" above), not yet against how Ayman actually says it or
  pauses before it. Whisper's rendering of other phrasings he might
  naturally use ("that's all," "that'll do it," etc.) isn't in
  `STOP_PHRASE_VARIANTS` yet — every match and near-miss is logged with
  the raw transcript in the chunk log specifically so real usage can
  extend that set from data.

```
.venv/bin/python daemon.py --simulate clip.wav     # drives the full state machine over a pre-recorded file
```

**Live-mic capture is implemented and has been used once, successfully**
(agent-supervised by gu2s6tnt, not this agent — two dictations processed
correctly end to end, under the prior acoustic stop-word mechanism since
superseded by the change above). `live()` is a real `sounddevice`
capture loop, and the cancel-socket server's detection logic
(`score_frame_ensemble` against a live-armed ensemble) is real, not a
placeholder. **Still not re-run since the stop-phrase change, and won't
be without Ayman's explicit go-ahead and presence each time** — per the
standing rule established after an unrelated incident during this build:
no unattended microphone capture, ever, live-mic testing only while
actively watched and stopped when watching stops. Four items from that
first session were explicitly **not** captured and remain owed to a
dedicated run: menu-bar mic-indicator visibility, `input_overflow` count,
a tally of acoustic-stage fires vs. verification-accepted starts, and
confirmation of which binary path the TCC microphone grant actually bound
to.

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
