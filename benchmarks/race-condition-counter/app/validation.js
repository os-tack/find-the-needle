/**
 * Input validation helpers.
 */

function isPositiveInteger(value) {
  const num = parseInt(value, 10);
  return !isNaN(num) && num > 0 && num.toString() === value.toString();
}

function validateIncrementBody(body) {
  if (body && body.amount !== undefined) {
    if (!isPositiveInteger(body.amount)) {
      return { valid: false, error: "amount must be a positive integer" };
    }
  }
  return { valid: true };
}

module.exports = { isPositiveInteger, validateIncrementBody };
