const Redis = require("ioredis");

let client = null;

/**
 * Create or return the singleton Redis client.
 */
function createClient() {
  if (!client) {
    client = new Redis({
      host: process.env.REDIS_HOST || "127.0.0.1",
      port: parseInt(process.env.REDIS_PORT || "6379", 10),
      maxRetriesPerRequest: 3,
      retryStrategy(times) {
        if (times > 3) return null;
        return Math.min(times * 200, 2000);
      },
    });

    client.on("error", (err) => {
      console.error("Redis connection error:", err.message);
    });

    client.on("connect", () => {
      console.log("Connected to Redis");
    });
  }
  return client;
}

/**
 * Close the Redis connection.
 */
async function closeClient() {
  if (client) {
    await client.quit();
    client = null;
  }
}

module.exports = { createClient, closeClient };
