const express = require("express");
const { createClient } = require("./redis-client");
const { incrementCounter, getCounter, resetCounter } = require("./counter");
const { logger } = require("./logger");

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());

app.post("/counter/increment", async (req, res) => {
  try {
    const value = await incrementCounter();
    res.json({ value });
  } catch (err) {
    logger.error("Increment failed:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

app.get("/counter", async (req, res) => {
  try {
    const value = await getCounter();
    res.json({ value });
  } catch (err) {
    logger.error("Get counter failed:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

app.post("/counter/reset", async (req, res) => {
  try {
    await resetCounter();
    res.json({ value: 0 });
  } catch (err) {
    logger.error("Reset failed:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

app.get("/health", (req, res) => {
  res.json({ status: "ok" });
});

app.listen(PORT, () => {
  logger.info(`Counter service listening on port ${PORT}`);
});

module.exports = app;
