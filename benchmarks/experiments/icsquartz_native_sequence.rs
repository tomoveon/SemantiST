use std::{borrow::Cow, fmt::Write};

use libafl::{
    corpus::Testcase,
    events::EventFirer,
    executors::ExitKind,
    feedbacks::Feedback,
    observers::ObserversTuple,
    state::State,
    Error, HasMetadata, SerdeAny,
};
use libafl_bolts::Named;
use serde::{Deserialize, Serialize};

extern "C" {
    fn ics_sequence_count() -> usize;
    fn ics_sequence_length(index: usize) -> usize;
    fn ics_sequence_data(index: usize) -> *const u8;
}

#[derive(Debug, SerdeAny, Serialize, Deserialize)]
pub struct NativeSequenceMetadata {
    pub raw_sequence_hex: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct NativeSequenceFeedback;

impl<S> Feedback<S> for NativeSequenceFeedback
where
    S: State + HasMetadata,
{
    fn is_interesting<EM, OT>(
        &mut self, _state: &mut S, _manager: &mut EM, _input: &S::Input,
        _observers: &OT, _exit_kind: &ExitKind,
    ) -> Result<bool, Error>
    where
        EM: EventFirer<State = S>,
        OT: ObserversTuple<S>,
    {
        Ok(false)
    }

    fn append_metadata<EM, OT>(
        &mut self, _state: &mut S, _manager: &mut EM, _observers: &OT,
        testcase: &mut Testcase<S::Input>,
    ) -> Result<(), Error>
    where
        EM: EventFirer<State = S>,
        OT: ObserversTuple<S>,
    {
        let count = unsafe { ics_sequence_count() };
        if count == 0 || count > 100 {
            return Ok(());
        }
        let mut frames = Vec::with_capacity(count);
        for index in 0..count {
            let length = unsafe { ics_sequence_length(index) };
            let data = unsafe { ics_sequence_data(index) };
            if length > (1 << 20) || (length != 0 && data.is_null()) {
                return Ok(());
            }
            let bytes = if length == 0 { &[][..] } else {
                unsafe { std::slice::from_raw_parts(data, length) }
            };
            let mut hex = String::with_capacity(length * 2);
            for byte in bytes {
                write!(&mut hex, "{byte:02x}").unwrap();
            }
            frames.push(hex);
        }
        testcase.add_metadata(NativeSequenceMetadata { raw_sequence_hex: frames });
        Ok(())
    }
}

impl Named for NativeSequenceFeedback {
    fn name(&self) -> &Cow<'static, str> {
        static NAME: Cow<'static, str> = Cow::Borrowed("NativeSequenceFeedback");
        &NAME
    }
}

impl NativeSequenceFeedback {
    pub fn new() -> Self { Self }
}
