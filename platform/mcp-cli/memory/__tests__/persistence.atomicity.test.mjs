import { after, before, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const testDataDir = path.join(__dirname, ".test-data-c8");

process.env.MCP_DATA_DIR = testDataDir;

const persistenceModule = await import(
  `${pathToFileURL(path.join(__dirname, "../../dist/memory/persistence.js")).href}?c8=${Date.now()}`
);

const {
  saveStore,
  loadStore,
  mutateStore,
  saveHealth,
  saveConfig,
  MEMORY_FILE,
  HEALTH_FILE,
  CONFIG_FILE,
  DATA_DIR,
} = persistenceModule;

const now = () => new Date().toISOString();

function makeEntry(key, content) {
  const timestamp = now();
  return {
    key,
    content,
    tags: [],
    namespace: "default",
    createdAt: timestamp,
    updatedAt: timestamp,
    accessCount: 0,
    lastAccessedAt: null,
    pinned: false,
  };
}

function resetDataDir() {
  fs.rmSync(DATA_DIR, { recursive: true, force: true });
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

before(() => {
  resetDataDir();
});

beforeEach(() => {
  resetDataDir();
});

after(() => {
  fs.rmSync(DATA_DIR, { recursive: true, force: true });
});

describe("memory persistence atomicity", () => {
  it("saveStore writes via tmp + rename and never leaves a half-written file", () => {
    saveStore({ version: 1, entries: { stable: makeEntry("stable", "before") } });

    const originalWriteFileSync = fs.writeFileSync;
    fs.writeFileSync = function patchedWriteFileSync(file, data, options) {
      if (String(file).startsWith(`${MEMORY_FILE}.tmp.`)) {
        throw new Error("simulated tmp write failure");
      }
      return originalWriteFileSync.call(this, file, data, options);
    };

    try {
      assert.throws(
        () => saveStore({ version: 1, entries: { broken: makeEntry("broken", "after") } }),
        /simulated tmp write failure/
      );
    } finally {
      fs.writeFileSync = originalWriteFileSync;
    }

    const raw = fs.readFileSync(MEMORY_FILE, "utf-8");
    const parsed = JSON.parse(raw);
    assert.equal(parsed.entries.stable.content, "before");
    assert.deepEqual(fs.readdirSync(DATA_DIR).filter((name) => name.includes(".tmp.")), []);
  });

  it("concurrent mutateStore calls preserve all writes", async () => {
    await Promise.all(
      Array.from({ length: 50 }, (_, index) => mutateStore((store) => {
        store.entries[`k${index}`] = makeEntry(`k${index}`, `v${index}`);
      }))
    );

    const store = loadStore();
    assert.equal(Object.keys(store.entries).length, 50);
    for (let index = 0; index < 50; index += 1) {
      assert.equal(store.entries[`k${index}`]?.content, `v${index}`);
    }
  });

  it("concurrent mutateStore on the same key keeps the last write", async () => {
    const executionOrder = [];

    await Promise.all(
      Array.from({ length: 20 }, (_, index) => mutateStore((store) => {
        executionOrder.push(index);
        store.entries.counter = makeEntry("counter", String(index));
      }))
    );

    assert.deepEqual(executionOrder, Array.from({ length: 20 }, (_, index) => index));
    const finalValue = loadStore().entries.counter?.content;
    assert.ok(new Set(Array.from({ length: 20 }, (_, index) => String(index))).has(finalValue));
    assert.equal(finalValue, String(executionOrder[executionOrder.length - 1]));
  });

  it("mutateStore survives a thrown mutator without breaking the chain", async () => {
    const results = await Promise.allSettled([
      mutateStore((store) => {
        store.entries.first = makeEntry("first", "ok");
      }),
      mutateStore(() => {
        throw new Error("boom");
      }),
      mutateStore((store) => {
        store.entries.third = makeEntry("third", "ok");
      }),
    ]);

    assert.equal(results[0].status, "fulfilled");
    assert.equal(results[1].status, "rejected");
    assert.equal(results[2].status, "fulfilled");

    const store = loadStore();
    assert.equal(store.entries.first?.content, "ok");
    assert.equal(store.entries.third?.content, "ok");
    assert.equal(store.entries.broken, undefined);
  });

  it("saveHealth and saveConfig also use atomic rename", () => {
    saveHealth({
      lastCompactAt: now(),
      lastCompactEntryCount: 2,
      pendingWrites: [],
      compactHistory: [],
    });
    saveConfig({
      baseStability: 10,
      accessBonus: 2.5,
      updateBonus: 5,
      fadingThreshold: 0.35,
      forgottenThreshold: 0.15,
      importantTags: { bug: 30 },
      importantNamespaces: { preferences: 60 },
    });

    assert.deepEqual(JSON.parse(fs.readFileSync(HEALTH_FILE, "utf-8")).pendingWrites, []);
    assert.equal(JSON.parse(fs.readFileSync(CONFIG_FILE, "utf-8")).baseStability, 10);
    assert.deepEqual(fs.readdirSync(DATA_DIR).filter((name) => name.includes(".tmp.")), []);
  });
});
