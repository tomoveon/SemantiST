// LibAFL saves the fuzzer state with postcard when an in-process client
// restarts after a crash. Postcard can neither skip fields nor decode
// serde_json::Value, so a type deriving its serde code with
// `#[serde(remote = "Self")]` and named here travels through binary formats
// as its JSON text. Human-readable formats (the bootstrap and report files)
// keep the derived layout.
macro_rules! serde_as_json_text_in_binary {
    ($ty:ty) => {
        impl serde::Serialize for $ty {
            fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                if serializer.is_human_readable() {
                    return <$ty>::serialize(self, serializer);
                }
                struct Derived<'a>(&'a $ty);
                impl serde::Serialize for Derived<'_> {
                    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                        <$ty>::serialize(self.0, serializer)
                    }
                }
                let text = serde_json::to_string(&Derived(self)).map_err(serde::ser::Error::custom)?;
                serializer.serialize_str(&text)
            }
        }

        impl<'de> serde::Deserialize<'de> for $ty {
            fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                if deserializer.is_human_readable() {
                    return <$ty>::deserialize(deserializer);
                }
                struct Derived($ty);
                impl<'de> serde::Deserialize<'de> for Derived {
                    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                        <$ty>::deserialize(deserializer).map(Derived)
                    }
                }
                let text = <String as serde::Deserialize>::deserialize(deserializer)?;
                serde_json::from_str::<Derived>(&text).map(|derived| derived.0).map_err(serde::de::Error::custom)
            }
        }
    };
}
