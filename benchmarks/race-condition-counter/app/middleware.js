const { logger } = require("./logger");

/**
 * Request logging middleware.
 */
function requestLogger(req, res, next) {
  const start = Date.now();
  res.on("finish", () => {
    const duration = Date.now() - start;
    logger.info(`${req.method} ${req.url} ${res.statusCode} ${duration}ms`);
  });
  next();
}

/**
 * Error handling middleware.
 */
function errorHandler(err, req, res, _next) {
  logger.error("Unhandled error:", err);
  res.status(500).json({ error: "Internal server error" });
}

module.exports = { requestLogger, errorHandler };
