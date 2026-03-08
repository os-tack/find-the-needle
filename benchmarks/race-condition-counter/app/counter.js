const { createClient } = require("./redis-client");
const { logger } = require("./logger");

const COUNTER_KEY = "app:counter";

/**
 * Increment the shared counter by 1.
 *
 * Reads the current value, adds 1, and writes it back.
 * This is the standard read-modify-write pattern.
 */
async function incrementCounter() {
  const client = createClient();

  // Read current value
  const current = await client.get(COUNTER_KEY);
  const value = parseInt(current || "0", 10);

  // Increment and write back
  const newValue = value + 1;
  await client.set(COUNTER_KEY, newValue.toString());

  logger.debug(`Counter incremented: ${value} -> ${newValue}`);
  return newValue;
}

/**
 * Get the current counter value.
 */
async function getCounter() {
  const client = createClient();
  const value = await client.get(COUNTER_KEY);
  return parseInt(value || "0", 10);
}

/**
 * Reset the counter to zero.
 */
async function resetCounter() {
  const client = createClient();
  await client.set(COUNTER_KEY, "0");
  logger.info("Counter reset to 0");
}

module.exports = { incrementCounter, getCounter, resetCounter };
