# Lumine browser certification — run summary

- Tests run: **7**
- Recorded scenarios: **7**
- Duration: **11.05 s**
- Status counts: `{"INTENTIONALLY_REFUSED": 1, "PASS": 3, "UNSUPPORTED": 3}`

## Capability matrix

| Phase | Area | Result | Counts |
| --- | --- | --- | --- |
| 10 | Dialogs & Permissions | **PARTIAL** | PASS=1, UNSUPPORTED=2 |
| 11 | Downloads & Uploads | **PARTIAL** | INTENTIONALLY_REFUSED=1, UNSUPPORTED=1 |
| 12 | Screenshots & Vision | **PASS** | PASS=2 |

## Scenarios

| ID | Status | Capability | Evidence |
| --- | --- | --- | --- |
| TC-10-01 | UNSUPPORTED | Native dialog handling (Unobserved Dialog Architecture) | Alert was auto-dismissed by driver without Lumine observing or recording message |
| TC-10-02 | PASS | Read and dismiss an in-page modal overlay | Named dialog was found and its close action removed the overlay. |
| TC-10-03 | UNSUPPORTED | Native confirm/prompt answers and beforeunload navigation | confirm='STATUS: confirm_result_false'; prompt='STATUS: prompt_result_null'; navigation through beforeunload=True |
| TC-11-01 | INTENTIONALLY_REFUSED | Download interception and deletion by design | _refuse_download event handler canceled and deleted download file |
| TC-11-02 | UNSUPPORTED | File upload capability absence | File inputs cannot be set via web_control or browse |
| TC-12-01 | PASS | Capture viewport screenshot with JPEG downscaling & PNG | Preview: 46118 bytes JPEG; Full: 38848 bytes PNG |
| TC-12-02 | PASS | The captured image reflects the page, and the preview is bounded | two pages produced different bytes=True; preview=37311 B JPEG=True; full=38848 B PNG=True; preview under cap=True; preview smaller=True |
