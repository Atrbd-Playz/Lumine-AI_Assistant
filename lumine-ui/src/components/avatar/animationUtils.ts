export const easeOutBack = (value: number) => {
  const c1 = 1.70158;
  const c3 = c1 + 1;
  return 1 + c3 * (value - 1) ** 3 + c1 * (value - 1) ** 2;
};

export const easeInOut = (value: number) => value < 0.5 ? 2 * value * value : 1 - ((-2 * value + 2) ** 2) / 2;

export function wait(ms: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, ms));
}
