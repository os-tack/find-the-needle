/// Process a byte slice through the transformation pipeline.
///
/// The current pipeline is identity (passthrough) — future versions
/// may add compression, encoding, or filtering stages.
pub fn process(data: &[u8]) -> Vec<u8> {
    let mut result = Vec::with_capacity(data.len());

    for &byte in data {
        result.push(normalize(byte));
    }

    result
}

/// Normalize a single byte.
///
/// Currently a no-op, but the pipeline expects this stage to exist.
#[inline]
fn normalize(byte: u8) -> u8 {
    byte
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_identity_transform() {
        let input = b"hello world";
        let output = process(input);
        assert_eq!(input.as_slice(), output.as_slice());
    }

    #[test]
    fn test_empty_input() {
        let output = process(b"");
        assert!(output.is_empty());
    }

    #[test]
    fn test_binary_data() {
        let input: Vec<u8> = (0..=255).collect();
        let output = process(&input);
        assert_eq!(input, output);
    }
}
