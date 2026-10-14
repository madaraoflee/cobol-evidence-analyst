The certificate and key in this directory are public synthetic test fixtures.
They identify only localhost and 127.0.0.1 and are used by the isolated desktop
acceptance provider. They are not credentials and must never be used to protect
a real service. The acceptance runner installs trust only for its child process.
No certificate-generation utility is required on the verification machine.
