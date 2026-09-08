import { useEffect, useRef } from "react";
import lumineAvatar from "../../../assets/lumine_face.svg";
import type { LumineState } from "../types";
import { clamp } from "../utils";

/** Avatar stage. The SVG stays external so its original eye transforms remain intact. */
export function Presence({ state, cursorGaze, showAvatarColor }: { state: LumineState; cursorGaze: boolean; showAvatarColor: boolean }) {
  const avatarRef = useRef<HTMLObjectElement>(null);
  useEffect(() => {
    const avatar = avatarRef.current;
    if (!avatar) return;
    let removeMouseMove: (() => void) | null = null;
    let animationFrame = 0;
    let wrappers: SVGGElement[] = [];
    const setup = () => {
      const document = avatar.contentDocument;
      if (!document) return;
      const eyes = Array.from(document.querySelectorAll<SVGGElement>(".oeil0, .oeil1"));
      if (eyes.length < 2) return;
      wrappers = eyes.slice(0, 2).map((eye) => {
        const existing = eye.parentElement?.closest("[data-lumine-gaze-wrapper]") as SVGGElement | null;
        if (existing) return existing;
        const wrapper = document.createElementNS("http://www.w3.org/2000/svg", "g");
        wrapper.setAttribute("data-lumine-gaze-wrapper", "true");
        eye.parentNode?.insertBefore(wrapper, eye);
        wrapper.appendChild(eye);
        return wrapper;
      });
      if (!cursorGaze) return;
      const svgElement = document.documentElement as unknown as SVGSVGElement;
      const viewBox = svgElement.viewBox.baseVal;
      const moveRange = Math.max(8, Math.min(viewBox.width, viewBox.height) * 0.035);
      let targetX = 0;
      let targetY = 0;
      let currentX = 0;
      let currentY = 0;
      const animate = () => {
        currentX += (targetX - currentX) * 0.14;
        currentY += (targetY - currentY) * 0.14;
        wrappers.forEach((wrapper) => wrapper.setAttribute("transform", `translate(${currentX} ${currentY}) rotate(${currentX * 0.08} ${viewBox.width / 2} ${viewBox.height / 2})`));
        animationFrame = requestAnimationFrame(animate);
      };
      const follow = (event: MouseEvent) => {
        const rect = avatar.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        targetX = clamp((event.clientX - (rect.left + rect.width / 2)) / (rect.width / 2), -1, 1) * moveRange;
        targetY = clamp((event.clientY - (rect.top + rect.height / 2)) / (rect.height / 2), -1, 1) * moveRange * 0.75;
      };
      window.addEventListener("mousemove", follow, { passive: true });
      animationFrame = requestAnimationFrame(animate);
      removeMouseMove = () => { window.removeEventListener("mousemove", follow); cancelAnimationFrame(animationFrame); };
    };
    const reset = () => { wrappers.forEach((wrapper) => wrapper.setAttribute("transform", "translate(0 0)")); removeMouseMove?.(); cancelAnimationFrame(animationFrame); };
    avatar.addEventListener("load", setup);
    if (avatar.contentDocument) setup();
    return () => { avatar.removeEventListener("load", setup); reset(); };
  }, [cursorGaze]);

  return <div className={`presence presence--${state} ${showAvatarColor ? "" : "presence--no-color"}`} aria-label={`Lumine is ${state}`}>
    <div className="presence-halo halo-one" /><div className="presence-halo halo-two" />
    <div className="presence-orbit orbit-one" /><div className="presence-orbit orbit-two" />
    <div className="presence-body"><object ref={avatarRef} data={lumineAvatar} type="image/svg+xml" aria-label="Lumine's animated avatar" /><div className="presence-glass" /></div>
  </div>;
}
