# silent-data-corruption

## Project

A Rust CLI tool (`fileproc`) that processes files through a transformation pipeline.

## Symptoms

When processing files above a certain size, the output file is smaller than the input. No error is reported. The tool exits successfully and prints a byte count to stderr, but the output is silently truncated. Small files work fine.

## Bug description

The file processing pipeline has a subtle data loss issue related to how it reads input. The tool reports success even when data is lost. The test creates a file large enough to trigger the problem and verifies the output matches the input byte-for-byte.

## Difficulty

Medium

## Expected turns

5-10
