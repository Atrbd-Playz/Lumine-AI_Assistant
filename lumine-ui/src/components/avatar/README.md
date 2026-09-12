# Lumine Avatar Animation

`LumineAvatarEngine` animates the existing `lumine_face.svg` in place. It never adds facial artwork: the original SVG remains the source of truth, while its two `.oeil0` / `.oeil1` groups and the embedded object transform provide expression, gaze, blinking, anticipation, overshoot, and settling.

The engine is intentionally independent from React. React changes emotion/activity and lab commands; `requestAnimationFrame` writes transforms directly to the SVG DOM. Add a new animation in `LumineAnimation`, then implement its timing in `LumineAvatarEngine.play`.
