export function shouldReduceMotion(
  env: Record<string, string | undefined> = process.env,
): boolean {
  const explicit = env.AGENTLOOM_REDUCED_MOTION?.trim().toLowerCase()
  return explicit === "1"
    || explicit === "true"
    || env.TERM?.trim().toLowerCase() === "dumb"
    || env.CI?.trim().toLowerCase() === "true"
}
