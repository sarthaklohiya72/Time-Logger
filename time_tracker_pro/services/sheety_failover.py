from __future__ import annotations

import logging
import requests
from typing import Optional, Tuple, Dict, Any

from ..repositories.sheety_accounts import (
    get_active_api_account,
    get_next_fallback_account,
    set_active_account,
    update_account_test_result,
    get_user_api_accounts
)
from ..core.rows import row_value


logger = logging.getLogger(__name__)


class SheetyFailoverService:
    """Service for handling Sheety API requests with automatic failover."""
    
    def __init__(self, db_name: str, user_id: int):
        self.db_name = db_name
        self.user_id = user_id
        self.max_retries = 3
        self.switched_account = False
        self.switched_from = None
        self.switched_to = None
    
    def _build_headers(self, api_token: Optional[str]) -> Dict[str, str]:
        """Build request headers with optional auth token."""
        headers = {'Content-Type': 'application/json'}
        if api_token:
            headers['Authorization'] = api_token if api_token.startswith('Bearer ') else f'Bearer {api_token}'
        return headers
    
    def _test_api_account(self, account) -> Tuple[bool, Optional[int], Optional[str]]:
        """Test if an API account is working. Returns (success, row_count, error_message)."""
        try:
            api_base_url = account['api_base_url']
            headers = self._build_headers(row_value(account, 'api_token'))
            
            response = requests.get(api_base_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    return False, None, "Sheety returned a non-JSON response"
                # Sheety returns data in format: {"sheet1": [{...}, {...}]}
                # Get the first key's value which should be the array of rows
                sheet_data = next(iter(data.values())) if data else []
                row_count = len(sheet_data) if isinstance(sheet_data, list) else 0
                return True, row_count, None
            else:
                preview = (response.text or "").strip().replace("\n", " ")
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                logger.warning(f"API test failed for account {account['id']}: HTTP {response.status_code}")
                detail = f"HTTP {response.status_code}"
                if preview:
                    detail = f"{detail} - {preview}"
                return False, None, detail
        except Exception as e:
            logger.error(f"API test error for account {account['id']}: {e}")
            return False, None, str(e)
    
    def _try_request(self, account, method: str, endpoint: str = '', json_data: Optional[Dict] = None) -> Tuple[bool, Optional[Any]]:
        """Try a request with a specific account. Returns (success, response_data)."""
        try:
            api_base_url = account['api_base_url']
            url = f"{api_base_url.rstrip('/')}/{endpoint.lstrip('/')}" if endpoint else api_base_url
            headers = self._build_headers(row_value(account, 'api_token'))
            
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=15)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, json=json_data, timeout=15)
            elif method.upper() == 'PUT':
                response = requests.put(url, headers=headers, json=json_data, timeout=15)
            elif method.upper() == 'DELETE':
                response = requests.delete(url, headers=headers, timeout=15)
            else:
                return False, None
            
            if response.status_code in (200, 201, 204):
                update_account_test_result(self.db_name, account['id'], True, self.user_id)
                try:
                    return True, response.json() if response.content else {}
                except:
                    return True, {}
            else:
                logger.warning(f"Request failed for account {account['id']}: HTTP {response.status_code}")
                update_account_test_result(self.db_name, account['id'], False, self.user_id)
                return False, None
        except Exception as e:
            logger.error(f"Request error for account {account['id']}: {e}")
            update_account_test_result(self.db_name, account['id'], False, self.user_id)
            return False, None
    
    def make_request(self, method: str, endpoint: str = '', json_data: Optional[Dict] = None) -> Tuple[bool, Optional[Any], Optional[str]]:
        """
        Make a Sheety API request with automatic failover.
        Returns (success, response_data, error_message).
        """
        # Get active account
        active_account = get_active_api_account(self.db_name, self.user_id)

        if not active_account:
            accounts = get_user_api_accounts(self.db_name, self.user_id)
            if not accounts:
                # No accounts configured
                return False, None, "No Sheety API accounts configured"
            for account in accounts:
                success, _, _ = self._test_api_account(account)
                update_account_test_result(self.db_name, account["id"], success, self.user_id)
                if success:
                    set_active_account(self.db_name, account["id"], self.user_id)
                    active_account = account
                    break
            if not active_account:
                return False, None, "No working Sheety API account found"
        
        # Try the active account
        success, data = self._try_request(active_account, method, endpoint, json_data)
        
        if success:
            return True, data, None
        
        # Active account failed, try every fallback account
        logger.info(f"Active account {active_account['id']} failed, trying failover...")

        accounts = get_user_api_accounts(self.db_name, self.user_id)
        if not accounts:
            return False, None, "No Sheety API accounts configured"

        attempts = 0
        for account in accounts:
            if account["id"] == active_account["id"]:
                continue
            attempts += 1
            logger.info(f"Trying fallback account {account['id']} (attempt {attempts})")
            success, data = self._try_request(account, method, endpoint, json_data)
            if success:
                # Failover successful! Switch to this account
                set_active_account(self.db_name, account["id"], self.user_id)
                self.switched_account = True
                self.switched_from = active_account["account_email"]
                self.switched_to = account["account_email"]

                logger.info(f"Failover successful: switched from {self.switched_from} to {self.switched_to}")

                return True, data, None

        # All accounts failed
        return False, None, "All API accounts failed"
    
    def get_failover_notification(self) -> Optional[Dict[str, str]]:
        """Get notification data if account was switched."""
        if self.switched_account:
            return {
                'from': self.switched_from,
                'to': self.switched_to
            }
        return None
    
    def test_connection(self, account_id: Optional[int] = None) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Test connection for a specific account or the active one.
        Returns (success, row_count, error_message).
        """
        if account_id:
            from ..repositories.sheety_accounts import get_api_account_by_id
            account = get_api_account_by_id(self.db_name, account_id, self.user_id)
        else:
            account = get_active_api_account(self.db_name, self.user_id)
        
        if not account:
            return False, None, "Account not found"
        
        success, row_count, error = self._test_api_account(account)
        update_account_test_result(self.db_name, account['id'], success, self.user_id)
        
        if success:
            return True, row_count, None
        else:
            return False, None, error or "Connection test failed"
