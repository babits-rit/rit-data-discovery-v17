# RIT Data Discovery Portal v1.4 — local prototype, no Entra required

This version removes Entra authentication requirements from v1.3 and retains the Data Marketplace, HERM map, glossary, steward directory, access request forms, and Excel/CSV imports.

## Run on your Mac

1. Unzip the folder and open Terminal in it.
2. Install Python 3 and the Excel import dependency (one-time):

   ```bash
   python3 -m pip install -r requirements.txt
   ```

3. Start the app:

   ```bash
   python3 app.py
   ```

4. Visit http://127.0.0.1:8765/ and select **Browse Data Marketplace**. Other pages: `/marketplace-admin`, `/glossary-admin`, `/stewards`, `/access-requests`.
5. Stop with Control+C in Terminal.

## Carry forward existing data

Stop the previous version. **Back up** its `catalog.db`, then copy that file into this folder before running v1.4. The app will initialize any new schema it needs. Do not copy a database while the old application is running.

## Important security warning

**Local testing only. No authentication or access controls are enabled.** Anyone with access to the local application can read or change glossary terms, catalog records, stewardship assignments, and access requests, including approvals. Enter **only fictitious or nonsensitive demonstration data**. Do not use for real RIT access requests or approvals. The application listens on 127.0.0.1 only; **do not** change it to 0.0.0.0, forward the port, or share it across a network. For multiuser hosting, add tested authentication, authorization, HTTPS, and database controls first.


## Version 1.5: Enterprise identifiers

- Every major governed record gets a permanent UUID and readable, type-prefixed display ID (`DATA-000001`, `TERM-000001`, etc.).
- Existing numeric primary keys and all foreign-key relationships are preserved. No destructive renumbering.
- HERM capabilities retain their BRM codes and gain deterministic UUIDs.
- Resource fields/columns have a new `resource_fields` registry with permanent UUIDs and display IDs. Existing field metadata is mirrored into the registry; it is not removed from the asset JSON.
- `/api/identifiers` returns the identifier registry for integration/testing. This administrative endpoint is local-only and must not be publicly exposed.
- Existing records are migrated automatically at startup. Newly created records receive identifiers before the next API read.
- For a safe upgrade, **back up `catalog.db` before copying it into this folder**. The migration is additive and is not intended to be rolled back.
- This version remains a local-only demonstration with no authentication; use nonsensitive test data.


## Version 1.6 — unified metadata relationships

Open **http://127.0.0.1:8765/relationships** to browse a unified metadata graph.

- All existing HERM, glossary, resource/field, marketplace, stewardship, and access-request associations are projected into the graph automatically.
- You can add manual typed relationships between any two registered objects; links use permanent UUIDs.
- Manual relationships are stored in `metadata_relationships`; derived relationships remain managed by their original workflow.
- The graph is exposed as JSON at `/api/relationships`.
- Existing database and IDs are preserved. Back up `catalog.db` before upgrading.
- This remains an unauthenticated **local-only prototype**, not suitable for real sensitive RIT data or shared/network use.
- No automatic lineage discovery or field-level impact calculation is implied; links are manually entered or derived from existing mappings.

## Version 1.7: structured use cases
The Data Access & Usage form now supports multiple independently stored use cases per request. Each use case has a stable UUID, readable `USE-000001` identifier, required name/description, Integration Hub topic, optional population/scope, frequency, retention, permitted and prohibited uses. The request itself continues to support HERM multi-select and an assigned steward. Existing free-text use cases are migrated into a single `Legacy use case` entry without deleting original text. Existing approval workflow is unchanged. This is a local prototype without verified authentication.


## Version 1.8 — Navigation

Open http://127.0.0.1:8765/menu for the main menu. A shared, responsive navigation bar is included on every HTML page, with Discover Data, Data Governance, and Administration menus. Existing routes and data structures are unchanged. No authentication is added.
