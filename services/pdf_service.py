import os
import io
import requests
from weasyprint import HTML
from pypdf import PdfWriter, PdfReader
from jinja2 import Environment, FileSystemLoader
from cryptography.fernet import Fernet
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def generate_invoice_pdf(invoice_data: dict, output_path: str):
    """
    Generate an invoice PDF from JSON data using Jinja2 and WeasyPrint,
    then append the payment.pdf.
    """
    # 1. Prepare data variables
    items = invoice_data.get('items', [])
    items_with_amounts = []
    total = 0.0
    
    for item in items:
        qty = float(item.get('qty', 0))
        # sansitize unit_price by removing SGD, $ and commas, then convert to float
        unit_price = float(str(item.get('unit_price', '0')).replace("SGD", "").replace("$", "").replace(",", "").strip())
        amount = qty * unit_price
        item['amount'] = amount
        items_with_amounts.append(item)
        total += amount

    # Setup Jinja2 environment
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    template_dir = os.path.join(base_dir, 'templates')
    assets_dir = os.path.join(base_dir, 'assets')
    
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template('invoice.html')
    
    # Path to logo - can be a URL or local path.
    # Instead use online URL for logo to prevent traces of local file paths in PDF metadata
    logo_uri = os.getenv('logo_url', '')

    # Pull co_reg_no from env variable
    co_reg_no = os.getenv('co_reg_no', '')
    invoice_data['co_reg_no'] = co_reg_no

    # Pull company data from env variables
    company_name = os.getenv('company_name', 'Your Company Name')
    company_address_line1 = os.getenv('company_address_line1', 'Your Company Address Line 1')
    company_address_line2 = os.getenv('company_address_line2', 'Your Company Address Line 2')
    company_address_line3 = os.getenv('company_address_line3', 'Your Company Address Line 3')
    
    # Render HTML
    html_out = template.render(
        invoice=invoice_data,
        items_with_amounts=items_with_amounts,
        total=total,
        logo_uri=logo_uri,
        company_name=company_name,
        company_address_line1=company_address_line1,
        company_address_line2=company_address_line2,
        company_address_line3=company_address_line3
    )
    
    # 2. Render main invoice PDF in memory
    pdf_bytes = HTML(string=html_out, base_url=base_dir).write_pdf()
    
    # 3. Unencrypt payment.enc with fernet_key .
    fernet_key = (
        os.getenv('FERNET_KEY')
        or os.getenv('fernet_key')
        or ''
    ).strip().encode()

    if not fernet_key:
        logger.error("Missing Fernet key. Set FERNET_KEY or fernet_key in the environment.")
        return None

    fernet = Fernet(fernet_key)
    logger.info(f"Fernet key length: {len(fernet_key)}")
    logger.info(f"BASE64 VALID: {len(fernet_key) == 44}")

    writer = PdfWriter()
    
    # Add main invoice PDF pages
    invoice_reader = PdfReader(io.BytesIO(pdf_bytes))
    for page in invoice_reader.pages:
        writer.add_page(page)
    
    try:
        try: # if cannot find the file, then error out with message
            payment_path = os.path.join(assets_dir, "payment.enc")
            with open(payment_path, "rb") as file:
                encrypted_payment = file.read()
                decrypted_pdf = fernet.decrypt(encrypted_payment)
                
                # Add decrypted payment pages
                payment_reader = PdfReader(io.BytesIO(decrypted_pdf))
                for page in payment_reader.pages:
                    writer.add_page(page)
                    logger.info("Added page from decrypted payment PDF.")
        except FileNotFoundError:
            logger.error("Error: payment.enc file not found in assets directory.")
            return None
        except Exception:
            logger.exception(
                "Error decrypting payment PDF. Check that payment.enc was re-encrypted with the exact same Fernet key deployed on Render."
            )
            return None
    except Exception as e:
        logger.exception("Error decrypting payment PDF")
        return None
        
    # Write the final PDF to disk
    with open(output_path, "wb") as f_out:
        writer.write(f_out)
        
    logger.info(f"Final PDF saved to {output_path}")
    return output_path
