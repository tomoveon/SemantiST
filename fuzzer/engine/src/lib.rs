//! In-process SemantiST fuzzer library. build_target.sh links it with one
//! target's harness and instrumented object into that target's fuzzer.
#![allow(dead_code)]
#![recursion_limit = "512"]

include!("campaign.rs");
include!("inprocess.rs");
