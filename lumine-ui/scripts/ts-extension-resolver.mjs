/**
 * Resolution hook for `check:profile-ops`.
 *
 * The application source uses extensionless relative imports, which Vite and
 * `tsc` both resolve and Node's ESM loader does not. This teaches Node to try
 * the TypeScript extension so the checks can run against the real source files
 * instead of a copy.
 *
 * Only used by the check script; nothing in the shipped bundle sees this.
 */

/** Import specifiers that already carry an extension Node understands. */
const HAS_EXTENSION = /\.[cm]?[jt]sx?$|\.json$/;

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith(".") && !HAS_EXTENSION.test(specifier)) {
    try {
      return await nextResolve(`${specifier}.ts`, context);
    } catch {
      // No TypeScript file at that path; let the default resolver report it.
    }
  }
  return nextResolve(specifier, context);
}
