# 0004 — Wake detection is keyword/phonetic, never a transcript match

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The obvious cheap wake implementation is: run speech-to-text on a window of audio
and look for her name in the text.

It fails in a way that is not a bug but an inevitability. Speech recognisers
transcribe "Lumine" as `lumin`, `dumin`, `lumina`, `loomin`, `Lumen`, "looming",
"a lumen" and a dozen other things, with a frequency that varies by accent,
microphone, background noise and how far the user is sitting from the keyboard. A
comparator built on those transcripts has an accuracy ceiling set by the recogniser
rather than by the detector, and it gets that ceiling *before* any wake logic runs.

This repository already knows the shape of the failure. The WhatsApp search path
was a verbatim substring test, and `'jubayed reza'.includes('jubayat')` is false
for one letter — a contact demonstrably on screen reported as absent. The fix was
not a looser substring; it was a *different mechanism* with a tolerance tuned
against real speech. The wake word has the same trap, larger, and it is the first
thing every user will judge the product on.

A second reason, independent of accuracy: a transcript-matching wake **requires a
recogniser to be running continuously**, which means cloud STT on silence — the
thing [0003](0003-wake-layer-separation.md) forbids.

## Decision

**Wake detection is a dedicated local keyword-spotting / phonetic detector. It
never depends on a speech-to-text transcript, and it never compares against a fixed
expected string.**

Requirements on the mechanism:

- **Phonetic or embedding-based, not exact-string.** It must score similarity, not
  equality.
- **Score, not boolean**, so a threshold is a configuration value rather than a
  constant buried in a port.
- **Several invocation styles**, because people say several things:
  `Hey Lumine` · `Lumine` · `Wake up Lumine` · `Wake Lumine` · `Arise, Lumine` ·
  `Lumine, wake up`. One hard-coded phrase is a demo.
- **A configurable threshold and a configurable phrase list**, in the settings
  document — because a user who cannot wake her has a product they cannot use, and
  a user on a noisy machine needs to loosen it without a rebuild.
- **A negative list** for cases that must never trigger playback of the name.

## Why not the alternative

Rejected: transcript matching, even as a first slice. It is tempting because the
pieces exist — an STT stage is already in the catalog. It is still wrong: it
inverts the privacy boundary, and its accuracy is the recogniser's, not ours.

Rejected: exact-substring matching on a name. Accuracy collapses on accents, and it
reads as a deliberate insult to users who do not share the developer's accent.

## Consequences

- Candidate engines must be **researched and compared before implementation**, not
  picked at the keyboard. Phase 1's first task is a written evaluation, covering:
  licence and commercial-use terms, offline operation, CPU cost while idle, model
  size, whether it can run in-process beside a Python worker without a second
  heavyweight runtime, and support for multiple phrases.
- Phrase lists and thresholds become product settings with a settings surface, which
  is one of the few settings screens justified by the vision.
- The detector is never allowed to become the reason wake is slow. A detector that
  takes 300ms is acceptable; one that takes 1s is indistinguishable from a
  microphone problem.

## Revisit when

An engine with materially better false-reject behaviour, or native
on-device detection, becomes available.