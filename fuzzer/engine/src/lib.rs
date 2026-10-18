//! In-process SemantiST fuzzer library. build_target.sh links it with one
//! target's harness and instrumented object into that target's fuzzer; it
//! shares every engine source with the forkserver binary (main.rs) through
//! campaign.rs, so code only the other entry point uses is not dead.
#![allow(dead_code)]

include!("campaign.rs");
include!("inprocess.rs");
