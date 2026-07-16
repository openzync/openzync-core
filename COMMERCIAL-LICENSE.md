# Commercial License

OpenZync is offered under a dual-license model:

## 1. Open-Source License (AGPL v3)

The core OpenZync platform is licensed under the **GNU Affero General Public License v3 (AGPL v3)**. This license allows you to:

- Use OpenZync internally for any purpose (commercial or non-commercial)
- Modify the software for your own use
- Distribute unmodified copies to others

**However**: If you modify OpenZync and offer it as a network service to third parties (i.e., a hosted/SaaS offering), the AGPL v3 requires you to release your modified source code to all users of that service.

## 2. Commercial License

If you wish to offer OpenZync as a hosted service (SaaS) WITHOUT releasing your modifications under AGPL v3, you need a **commercial license**. A commercial license:

- Allows you to offer OpenZync as a SaaS without open-sourcing your modifications
- Provides warranty and indemnification
- Includes priority support and direct access to the core engineering team
- Covers all proprietary components (services/, workers/, prompts/, core/)

## Getting a Commercial License

For commercial licensing inquiries, contact:

- **Email:** rohnsha0@gmail.com
- **Website:** https://github.com/rohnsha0/openzync

## License Comparison

| Feature | AGPL v3 | Commercial License |
|---------|---------|-------------------|
| Internal use | ✅ Free | ✅ Included |
| Modify for own use | ✅ Free | ✅ Included |
| Distribute unmodified copies | ✅ Allowed | ✅ Allowed |
| Offer as SaaS without releasing source | ❌ Must release modifications | ✅ Allowed |
| Warranty & indemnification | ❌ No | ✅ Yes |
| Priority support | ❌ Community only | ✅ Included |
| Access to proprietary components | ✅ Source-available | ✅ Full access |

## Other Components

Components outside the core server are licensed permissively — not AGPL — to encourage adoption and integration:

| Component | License | Use |
|-----------|---------|-----|
| Python SDK (`openzync-sdk-python`) | Apache 2.0 | Client library for integrating with OpenZync from Python |
| MCP Server (`openzync-mcp`) | Apache 2.0 | Model Context Protocol server for AI agent tool integration |
| Web Frontend (`openzync-frontend`) | MIT | Next.js-based user interface |
| Landing Page (`openzync-landing`) | MIT | Marketing & documentation site |
| Documentation (`openzync-docs`) | CC-BY-4.0 | User guides and API reference |

You can integrate, modify, and distribute these components without restriction, including in proprietary applications.

---

*This document is for informational purposes and does not constitute a legal agreement. For the full license terms, see [LICENSE](./LICENSE).*
