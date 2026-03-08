use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::path::Path;
use std::process;

mod transform;

const BUFFER_SIZE: usize = 65536; // 64KB

fn process_file(input_path: &str, output_path: &str) -> io::Result<()> {
    let mut file = fs::File::open(input_path)?;
    let mut buffer = [0u8; BUFFER_SIZE];

    // Read file into fixed buffer — silently truncates if file > 64KB
    let bytes_read = file.read(&mut buffer)?;

    let data = &buffer[..bytes_read];
    let transformed = transform::process(data);

    let mut output = fs::File::create(output_path)?;
    output.write_all(&transformed)?;

    eprintln!(
        "Processed {} -> {} ({} bytes)",
        input_path, output_path, transformed.len()
    );

    Ok(())
}

fn print_usage() {
    eprintln!("Usage: fileproc <input> <output>");
    eprintln!("       fileproc --stats <input>");
    eprintln!();
    eprintln!("Process a file through the transformation pipeline.");
}

fn print_stats(path: &str) -> io::Result<()> {
    let metadata = fs::metadata(path)?;
    let size = metadata.len();

    println!("File: {}", path);
    println!("Size: {} bytes", size);
    println!("Size (KB): {:.2}", size as f64 / 1024.0);

    if size > BUFFER_SIZE as u64 {
        println!("Warning: File exceeds internal buffer size of {} bytes", BUFFER_SIZE);
    }

    Ok(())
}

fn main() {
    let args: Vec<String> = env::args().collect();

    match args.len() {
        2 => {
            eprintln!("Error: missing output path");
            print_usage();
            process::exit(1);
        }
        3 => {
            if args[1] == "--stats" {
                if let Err(e) = print_stats(&args[2]) {
                    eprintln!("Error: {}", e);
                    process::exit(1);
                }
            } else {
                let input = &args[1];
                let output = &args[2];

                if !Path::new(input).exists() {
                    eprintln!("Error: input file '{}' not found", input);
                    process::exit(1);
                }

                if let Err(e) = process_file(input, output) {
                    eprintln!("Error processing file: {}", e);
                    process::exit(1);
                }
            }
        }
        _ => {
            print_usage();
            process::exit(1);
        }
    }
}
