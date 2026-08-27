# Temporary and Disposable Smoke-Test Artifact: Reduced Feature Workflow

> **IMPORTANT**: This is a temporary and disposable smoke-test artifact. It carries no runtime impact and can be safely removed once the reduced declarative Feature workflow is validated.

## Overview
This document serves as a controlled, minimal-scope work item to validate the reduced declarative Feature workflow pipeline end-to-end. It isolates workflow behavior from complex implementation details by serving as a trivial target artifact.

## Workflow Stages

### Verified / Included Stages
The reduced declarative Feature workflow validates the following stages end-to-end:
- PRD Generation
- Specification Generation
- Implementation
- Local Review
- Pull Request Creation

### Skipped / Excluded Stages
To provide a streamlined and faster validation loop, the following planning and decomposition stages are explicitly skipped and excluded:
- Epic Decomposition
- Plan Approval
- Task Generation

## Disposability Note
This artifact and its parent directory (`docs/testing/`) can be deleted from the repository at any time. There are no external references or dependencies within the codebase, and deleting it causes zero test or build failures.
