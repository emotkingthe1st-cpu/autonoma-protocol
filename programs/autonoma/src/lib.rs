use anchor_lang::prelude::*;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

declare_id!("Auto111111111111111111111111111111111111111");

#[program]
pub mod autonoma {
    use super::*;

    pub fn initialize_genesis(ctx: Context<InitializeGenesis>) -> Result<()> {
        let state = &mut ctx.accounts.protocol_state;
        state.is_initialized = true;
        state.max_tx_bps = 50;       // 0.50% Max Transaction constraint
        state.max_wallet_bps = 150;  // 1.50% Max Wallet constraint
        state.total_burned = 0;
        state.transition_count = 0;
        
        msg!("AUTONOMA: Genesis Invariants Initialized On-Chain.");
        Ok(())
    }

    pub fn execute_state_transition(ctx: Context<ExecuteStateTransition>, amount: u64) -> Result<()> {
        let state = &mut ctx.accounts.protocol_state;
        require!(state.is_initialized, AutonomaError::UninitializedState);

        // Instruction-level invariant validation
        let max_tx_limit = (1_000_000_000 * 10u64.pow(9) * state.max_tx_bps as u64) / 10_000;
        require!(amount <= max_tx_limit, AutonomaError::MaxTxLimitExceeded);

        state.transition_count = state.transition_count.checked_add(1).unwrap();
        msg!("AUTONOMA: State Transition #{} Executed.", state.transition_count);
        Ok(())
    }
}

#[derive(Accounts)]
pub struct InitializeGenesis<'info> {
    #[account(
        init,
        payer = authority,
        space = 8 + ProtocolState::INIT_SPACE
    )]
    pub protocol_state: Account<'info, ProtocolState>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ExecuteStateTransition<'info> {
    #[account(mut)]
    pub protocol_state: Account<'info, ProtocolState>,
    pub signer: Signer<'info>,
}

#[account]
#[derive(InitSpace)]
pub struct ProtocolState {
    pub is_initialized: bool,
    pub max_tx_bps: u16,
    pub max_wallet_bps: u16,
    pub total_burned: u64,
    pub transition_count: u64,
}

#[error_code]
pub enum AutonomaError {
    #[msg("Protocol state machine is uninitialized.")]
    UninitializedState,
    #[msg("Instruction exceeds hardcoded Max Tx limit (0.5%).")]
    MaxTxLimitExceeded,
    #[msg("Destination account exceeds Max Wallet invariant (1.5%).")]
    MaxWalletExceeded,
}
