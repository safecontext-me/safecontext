// Worker entry point. The Workers runtime only accepts handlers and Durable Object classes as
// exports of the main module, so the code (and the helpers the tests import) lives in relay.js.
export { default, LinkRelay } from "./relay.js";
