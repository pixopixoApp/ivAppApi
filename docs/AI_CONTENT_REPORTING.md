# AI content reporting

Google Play's AI-generated content policy requires an in-app way to report
or flag offensive AI-generated content to the developer, without leaving the
app. Reports should inform content filtering and moderation:
https://support.google.com/googleplay/android-developer/answer/13985936

## Paths

- The feed's Report control reports the displayed published work. Work detail
  and QR views expose reporting through their options menu. Creators can report
  their own published works.
- In the creator flow, Report on the generated source preview reports its
  generation attempt. Story review and generation show Report for each ready
  generated ending; source A reports the source generation when it is AI-made.
  The interaction plan and playtest report the corresponding creator version.
- The server verifies that private AI results belong to the authenticated
  reporter. Reports enter the existing moderation queue with a stable attempt
  or version ID; they do not automatically take down content.
- Moderators can filter by AI clip or AI version, inspect generation metadata,
  open an available clip preview, and record a decision. They should use the
  records to investigate patterns and improve safety filters.

## Release and review

Deploy the application backend and management API before the Android client,
then deploy the management UI and publish the Android update. Check that the
public share base URL points to the app API so moderation preview links work.
Verify on a real device with a test account: report a feed item, an AI source,
a generated story ending, and an interactive preview; confirm each appears in
the moderation queue, open an available generated clip, and record a decision.
Include the path and an account with access to AI generation in the Play review
instructions. Policy compliance and reinstatement are determined by Google Play.
