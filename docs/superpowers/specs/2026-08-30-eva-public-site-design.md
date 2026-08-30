# Eva Public Site Design

Date: 2026-08-30
Status: Approved for implementation
Domain: `evaatyourservice.com`

## Objective

Create a polished public website for Eva that explains the product vision, accurately represents the current private-beta state, and supplies the stable homepage, privacy-policy, and terms-of-service URLs required to publish Eva's Google OAuth application.

## Decisions

- Host a dependency-free static site with GitHub Pages.
- Keep the site in `site/` so product code and public-site assets remain clearly separated.
- Deploy through GitHub Actions rather than a long-lived generated branch.
- Configure the apex domain through a committed `CNAME` file containing `evaatyourservice.com`.
- Use only local HTML, CSS, SVG, and minimal JavaScript. Do not load analytics, cookies, trackers, external fonts, or third-party scripts.
- Present Eva as a private-beta product in active development. Do not offer public signup or imply that unfinished features are currently available.
- Use the GitHub repository as the public contact route instead of publishing the owner's personal email address.
- Describe Gmail access as read-only and disclose the categories of email data Eva processes. Avoid unsupported claims such as end-to-end encryption or zero data retention.
- Use a restrained dark visual system with warm violet and coral accents, strong typography, soft ambient gradients, and a code-native SVG identity mark.

## Scope

### Included

- Public homepage at `/`
- Privacy policy at `/privacy/`
- Terms of service at `/terms/`
- Custom 404 page
- Shared visual assets and navigation
- Search and social metadata
- `robots.txt` and `sitemap.xml`
- GitHub Pages custom-domain configuration
- GitHub Actions deployment workflow
- Automated structural, content, link, and deployment tests
- Local preview documentation

### Excluded

- User registration, authentication, dashboards, or public Gmail connection
- Contact forms, newsletters, analytics, cookies, or consent banners
- Backend APIs or coupling the site deployment to Eva's FastAPI service
- Public claims that Telegram, autonomous actions, or every planned integration are already available
- Domain DNS changes, Search Console verification, Google OAuth publishing, and Gmail credential rotation; these remain operator actions after deployment

## Information Architecture

Every page uses a consistent header and footer. The header contains the Eva mark, product name, and links to the homepage, privacy policy, terms, and source repository. Policy links remain visible without JavaScript.

### Homepage

The homepage has six concise sections:

1. **Hero** — positions Eva as a proactive personal AI that notices what matters and brings it to the user at the right moment. A private-beta badge prevents the page from implying public availability.
2. **Product principle** — explains that Eva observes permitted signals, connects context, and asks before consequential actions.
3. **How Eva works** — a three-step sequence: understand signals, connect them to goals, and surface a clear next step.
4. **Current foundation** — accurately states that read-only Gmail ingestion and the durable event backbone exist today, while Telegram interaction, contextual reasoning, and approved actions are being built in phases.
5. **Trust principles** — highlights least-privilege access, explicit approval, durable provenance, and separation of external content from user intent.
6. **Closing statement** — reinforces the long-term vision without a signup call to action.

### Privacy Policy

The privacy policy is written for the current private-beta implementation and covers:

- operator and applicability;
- data categories: Google account identity, Gmail message metadata and content, attachment metadata, OAuth authorization material, operational records, and user-provided interaction data when later channels are enabled;
- purposes: connecting the mailbox, detecting relevant events, maintaining service reliability, security, and user-requested assistance;
- Gmail scope: read-only access and no ability to send, delete, or modify email;
- credential handling: OAuth authorization material is stored separately from application data using managed secret infrastructure;
- data sharing: infrastructure providers needed to operate Eva, legal requirements, and no sale of personal data;
- retention and deletion: retention only as needed for the private beta, with deletion requests handled through the public repository contact route;
- security limitations, international processing, children's privacy, policy changes, and contact;
- an explicit statement that Google user data use follows the Google API Services User Data Policy, including Limited Use requirements.

The policy must describe actual behavior and avoid commitments the current system cannot yet enforce automatically.

### Terms of Service

The terms explain:

- Eva is an experimental private-beta assistant;
- use is permitted only when authorized by the operator;
- users remain responsible for decisions and reviewing suggestions;
- the service is not professional legal, medical, financial, or emergency advice;
- misuse, interference, credential abuse, and unlawful activity are prohibited;
- integrations may be suspended or revoked;
- intellectual-property ownership and third-party service terms;
- availability is not guaranteed and the service is provided without warranties to the extent permitted by law;
- liability is limited to the extent permitted by applicable law;
- terms may change, with the effective date shown on the page;
- the repository is the contact route.

## Visual and Interaction Design

- Use semantic landmarks, a logical heading hierarchy, skip navigation, visible keyboard focus, and sufficient contrast.
- Support mobile, tablet, and desktop layouts without horizontal scrolling.
- Respect `prefers-reduced-motion`; all content remains understandable with animation disabled.
- Use subtle CSS-only entrance effects and ambient background shapes. JavaScript may only enhance presentation and must not be required for navigation or content.
- Keep body copy readable with a constrained line length and generous spacing.
- Use an inline or local SVG mark so the brand remains crisp without a raster dependency.
- Provide a clear active-page treatment in policy navigation.

## File Layout

```text
site/
├── 404.html
├── CNAME
├── index.html
├── privacy/
│   └── index.html
├── terms/
│   └── index.html
├── assets/
│   ├── favicon.svg
│   ├── mark.svg
│   ├── site.js
│   └── styles.css
├── robots.txt
└── sitemap.xml

.github/workflows/pages.yml
tests/unit/site/test_public_site.py
```

## Deployment

The Pages workflow runs on pushes to `main` and supports manual dispatch. It checks out the repository, configures Pages, uploads `site/` as the artifact, and deploys it with the minimum required GitHub token permissions. The deploy job uses the `github-pages` environment and exposes the deployment URL.

The repository's Pages source must be set to GitHub Actions. After the first deployment, the operator will add the documented Hostinger DNS records, enable HTTPS when GitHub makes it available, and verify both the apex and `www` routes.

## Testing

Tests use Python's standard library and do not add a site framework or parser dependency. They verify:

- every required page and asset exists;
- each page has a non-empty title, description, canonical URL, one primary heading, and shared navigation/footer links;
- internal absolute paths resolve to committed files;
- policy pages contain their required disclosure topics and an effective date;
- the privacy page identifies the read-only Gmail scope and Google Limited Use requirements;
- no page references analytics, trackers, external scripts, or insecure HTTP assets;
- `CNAME`, `robots.txt`, and `sitemap.xml` contain the production domain;
- the Pages workflow deploys the `site/` directory and has constrained permissions;
- existing backend formatting, typing, unit, and integration checks remain green.

Visual verification uses a local static server at representative mobile and desktop viewport widths. Navigation, focus states, policy readability, reduced-motion behavior, and the 404 page are inspected before handoff.

## Error Handling and Resilience

- The site has no runtime service dependencies, so policy pages remain available independently of Eva's backend.
- Links use stable root-relative or canonical production URLs so nested policy pages work on the custom domain.
- The custom 404 page returns visitors to the homepage and policy pages.
- Essential navigation and content work when JavaScript is unavailable.
- Deployment failure leaves the previous successful Pages version online.

## Acceptance Criteria

The change is complete when:

1. The homepage, privacy policy, and terms pages render correctly from `site/` at their final production paths.
2. Copy is accurate to Eva's current private-beta capabilities and planned direction.
3. Gmail data handling, read-only access, credential separation, Google Limited Use, retention, deletion, and contact are disclosed without unsupported guarantees.
4. The site is accessible, responsive, tracker-free, and usable without JavaScript.
5. The GitHub Pages workflow deploys only the static site with minimum required permissions.
6. Automated site tests and the existing repository verification suite pass.
7. A local visual inspection finds no layout, navigation, focus, or readability defects at mobile and desktop widths.
8. The feature branch is pushed and a pull request is opened against `main`; it is not merged without explicit approval.

## Post-Merge Operator Steps

1. Set the repository's Pages build source to GitHub Actions if GitHub has not selected it automatically.
2. Configure GitHub Pages custom domain `evaatyourservice.com` before changing DNS.
3. Add the four GitHub Pages apex `A` records and the `www` CNAME in Hostinger without changing mail-related records.
4. Wait for DNS validation and enable Enforce HTTPS.
5. Verify `evaatyourservice.com` in Google Search Console.
6. Add the homepage, privacy, and terms URLs plus the authorized domain in Google Auth Platform.
7. Publish the OAuth app to production.
8. Reauthorize Eva's Gmail connector so it receives a production refresh token.
