import { defineWorkersConfig } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig({
  test: {
    poolOptions: {
      workers: {
        // Durable Object WebSockets don't work with per-test isolated storage;
        // every test uses its own random link instead.
        isolatedStorage: false,
        singleWorker: true,
        wrangler: { configPath: "./wrangler.toml" },
        miniflare: { bindings: { ALLOWED_REDIRECT_HOSTS: "grok.com,x.ai" } },
      },
    },
  },
});
