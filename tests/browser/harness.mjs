/** One browser per suite; each case owns and closes all of its contexts. */
import { pathToFileURL } from "node:url";

let shared;
let activeFixtures;
const errors = new WeakMap();
export const pageErrors = (page) => errors.get(page) || [];

async function launch() {
  if (!process.env.PLAYWRIGHT_MODULE)
    throw Error("Set PLAYWRIGHT_MODULE to the development Playwright index.mjs");
  const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
  return chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    args: ["--no-sandbox"],
    env: {
      ...process.env,
      LD_LIBRARY_PATH: [process.env.TURING_BROWSER_LIBRARY_PATH, process.env.LD_LIBRARY_PATH].filter(Boolean).join(":"),
    },
  });
}

export async function browserSession(run) {
  if (shared) throw Error("Browser sessions cannot overlap");
  shared = await launch();
  try { return await run(); }
  finally { await shared.close(); shared = undefined; }
}

export async function browserScenario(run, milliseconds = 90000) {
  if (activeFixtures) throw Error("Browser scenarios cannot overlap");
  const fixtures = activeFixtures = new Set();
  let timer;
  try {
    return await Promise.race([
      run(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(Error(`Browser scenario exceeded ${milliseconds / 1000}s`)), milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timer);
    await Promise.all([...fixtures].map((fixture) => fixture.close()));
    activeFixtures = undefined;
  }
}

export async function browserFixture() {
  const browser = shared || await launch();
  const standalone = browser !== shared;
  const contexts = new Set();
  async function newContext(options) {
    const context = await browser.newContext(options);
    context.on("page", (page) => {
      const messages = [];
      errors.set(page, messages);
      page.on("pageerror", (error) => {
        messages.push(error.message);
        if (messages.length > 8) messages.shift();
      });
    });
    contexts.add(context);
    return context;
  }
  const owner = activeFixtures;
  const fixture = {
    newContext,
    async newPage(options) { return (await newContext(options)).newPage(); },
    async close() {
      owner?.delete(fixture);
      try { await Promise.all([...contexts].map((context) => context.close())); }
      finally { if (standalone) await browser.close(); }
    },
  };
  owner?.add(fixture);
  return fixture;
}
