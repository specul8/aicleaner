# Moving a domain's DNS from cPanel to Cloudflare

[Home](Home.md) > [Cloudflare tunnel](Cloudflare-Tunnel.md) > Moving DNS from cPanel

For remote access the domain's DNS has to be run by Cloudflare. If your web host gives you cPanel,
this is how to move it without disturbing email or your website. It changes **who answers** for the
domain, not where your website or email live - and it can be undone by putting the old nameservers
back.

Not sure? A separate, cheap domain bought at Cloudflare avoids all of this: nothing about your main
domain changes.

## 1. Keep a copy of every record

1. Sign in to cPanel and open **Zone Editor** (under Domains).
2. Choose the domain and **Manage**. If your host offers **Export** (sometimes under Advanced DNS
   Zone Editor), download the zone file. Otherwise copy down every record: its type, name and value.
3. Note especially the **MX** records (email), any **TXT** records (SPF, DKIM, DMARC and domain
   verifications), and the **A** or **CNAME** records for the website and `www`.

## 2. Add the domain to Cloudflare

1. In the Cloudflare dashboard choose **Add a domain**, enter it, and pick the **Free** plan.
2. Cloudflare scans the existing records. Compare its list with your copy and add whatever is missing
   - or import the zone file under **DNS > Records > Import and Export**.
3. For the mail records (MX, and the autodiscover or mail CNAMEs) make sure the cloud is **grey** (DNS
   only), not orange: mail must not go through Cloudflare's proxy.

## 3. Change the nameservers

1. Cloudflare shows two nameservers, such as `ada.ns.cloudflare.com`.
2. Where the domain is registered - often the same host, under **Domains** or **Nameservers** in its
   client area rather than in cPanel itself - replace the current nameservers with those two.
3. Wait until Cloudflare shows the domain as **Active**: minutes to a few hours. Email and the website
   keep working throughout, because both sets of nameservers give the same answers.

## Undoing it

Put the host's original nameservers back at the registrar. Nothing was deleted at the host, so
everything answers from there again once the change spreads.

Then go back to the setup wizard's Remote access step and look the domain up again.
