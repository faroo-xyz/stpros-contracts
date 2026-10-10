// Retained CLI tags only. Historical deployment records are append-only.
// No imports or runtime reads: refusing must precede deployment/wallet access.
export default Object.assign(
  async () => {
    throw new Error("Deprecated deploy/SlpDistributor.ts: legacy deployment is incompatible with R4. Use the reviewed release process; this entry point cannot deploy or overwrite records.");
  },
  { tags: ["SlpDistributor", "SlpDistributor_deploy"] },
);
